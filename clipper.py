#!/usr/bin/env python3
"""
GTA 6 Klip Botu v2  -  Content Rewards kampanyaları için
-------------------------------------------------------
1. campaigns.yaml'daki her kampanya için yayıncının Twitch'teki GTA 6 yayınlarını bulur
2. Patlama anlarını izleyicilerin kestiği Twitch kliplerinden seçer
   (yoksa yayının sesinden bulur)
3. O anı resmi yayın kaydından KENDİSİ keser (başkasının klibini yeniden yüklemez)
4. 9:16 dikey yapar, İngilizce hook yazısı + İngilizce altyazı ekler,
   kampanya istiyorsa resmi logoyu ekler (istemiyorsa hiçbir logo eklemez)
5. Kampanya kurallarını kontrol eder; uymayan klibi atar
6. Hazır klipleri + başlık/açıklama metinlerini GitHub Releases sayfasına koyar

Kullanım:
  python clipper.py                    # normal çalışma
  python clipper.py --dry-run          # sadece hangi anları seçeceğini yazdır
  python clipper.py --test-render v.mp4 --campaign kreekcraft
"""
import argparse
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
OUT, TMP, ASSETS = ROOT / "output", ROOT / "tmp", ROOT / "assets"
STATE_FILE = ROOT / "state.json"
TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"   # Twitch web sitesinin herkese açık istemci kimliği

# Kampanyalar: yayıncıyı olumsuz gösteren, drama/nefret içerikli klipler yasak
BLOCK_WORDS = ["racist", "exposed", "cancel", "drama", "beef", "hate", "nigg", "fag",
               "retard", "kys", "kill yourself", "slur", "http", "www."]


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


# ================================================================ Twitch
def twitch_gql(query, variables):
    import requests
    r = requests.post("https://gql.twitch.tv/gql",
                      headers={"Client-ID": TWITCH_CLIENT_ID, "Content-Type": "text/plain"},
                      data=json.dumps({"query": query, "variables": variables}), timeout=30)
    r.raise_for_status()
    j = r.json()
    if j.get("errors"):
        raise RuntimeError(j["errors"][0].get("message"))
    return j["data"]


Q_VIDEOS = """query($l:String!){ user(login:$l){
  videos(first:10, type:ARCHIVE, sort:TIME){ edges{ node{
    id title lengthSeconds createdAt status game{ name } } } } } }"""

Q_CLIPS = """query($l:String!){ user(login:$l){
  clips(first:50, criteria:{period:LAST_DAY, sort:VIEWS_DESC}){ edges{ node{
    slug title viewCount durationSeconds createdAt game{ name }
    video{ id } videoOffsetSeconds } } } } }"""


def parse_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def in_window(dt, camp):
    if camp.get("start_date") and dt < parse_dt(camp["start_date"] + "T00:00:00Z"):
        return False
    if camp.get("end_date") and dt > parse_dt(camp["end_date"] + "T23:59:59Z"):
        return False
    return True


def campaign_open(camp):
    return in_window(datetime.now(timezone.utc), camp)


def eligible_vods(camp, d):
    """Kampanyanın kabul ettiği yayınlar: doğru oyun + tarih aralığı."""
    vods = {}
    data = twitch_gql(Q_VIDEOS, {"l": camp["twitch"]})
    for e in (data["user"] or {}).get("videos", {}).get("edges", []):
        v = e["node"]
        game = (v.get("game") or {}).get("name", "")
        if game != d["game"]:
            continue
        if not in_window(parse_dt(v["createdAt"]), camp):
            continue
        vods[v["id"]] = v
    return vods


def clean_title(t):
    t = re.sub(r"\s+", " ", (t or "")).strip(" \"'“”")
    if len(re.sub(r"[^A-Za-z]", "", t)) < 6 or len(t.split()) < 3:   # "j", "Gg", "jeff gta6" gibi
        return ""                                                     # zayıf başlık -> yayıncının sözü kullanılır
    if any(b in t.lower() for b in BLOCK_WORDS):
        return None                                   # None = bu klibi hiç kullanma
    return t


def moments_from_clips(camp, d, vods):
    """Twitch izleyicilerinin kestiği klipler = yayının en iyi anları."""
    data = twitch_gql(Q_CLIPS, {"l": camp["twitch"]})
    moments = []
    for e in (data["user"] or {}).get("clips", {}).get("edges", []):
        c = e["node"]
        vid = (c.get("video") or {}).get("id")
        if not vid or vid not in vods or c.get("videoOffsetSeconds") is None:
            continue
        if (c.get("game") or {}).get("name") != d["game"]:
            continue
        if c["viewCount"] < d["min_clip_views"]:
            continue
        title = clean_title(c["title"])
        if title is None:
            continue
        start = max(0, c["videoOffsetSeconds"] - d["clip_lead_seconds"])
        length = min(d["max_seconds"], max(d["min_seconds"],
                     c["durationSeconds"] + d["clip_lead_seconds"] + d["clip_tail_seconds"]))
        # aynı anı iki kez kesme
        if any(m["vod"] == vid and abs(m["start"] - start) < 45 for m in moments):
            continue
        moments.append({"key": f"{camp['id']}_{vid}_{int(start)}", "vod": vid,
                        "start": int(start), "length": int(length),
                        "hook": title, "views": c["viewCount"]})
    return moments


# ================================================================ Video
def run(cmd, cwd=None, timeout=900):
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"{Path(cmd[0]).name} çıkış kodu {r.returncode}: "
                           + (r.stderr or r.stdout or "")[-1500:])
    return r.stdout


def hls_segments(playlist_url, start, length):
    """Twitch yayın listesinden sadece [start, start+length] aralığını kapsayan parçaları seçer."""
    import requests
    from urllib.parse import urljoin
    text = requests.get(playlist_url, timeout=30).text
    if "#EXT-X-STREAM-INF" in text:                       # ana liste -> ilk (en iyi) kalite
        lines = text.splitlines()
        i = next(k for k, l in enumerate(lines) if l.startswith("#EXT-X-STREAM-INF"))
        playlist_url = urljoin(playlist_url, lines[i + 1].strip())
        text = requests.get(playlist_url, timeout=30).text
    segs, t, dur = [], 0.0, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            dur = float(line[8:].split(",")[0])
        elif line and not line.startswith("#") and dur is not None:
            if t + dur > start and t < start + length:
                segs.append((t, urljoin(playlist_url, line)))
            t += dur
            dur = None
    if not segs:
        raise RuntimeError(f"bu zaman aralığı yayında yok (yayın {t:.0f} sn)")
    return segs


def download_section(vod_id, start, length, dest):
    """Yayının tamamını değil, sadece o parçayı indirir: ilgili .ts parçaları + yerel kesim."""
    import requests
    out = run(["yt-dlp", "--no-warnings", "-g", "-f", "best[height<=1080]/best",
               f"https://www.twitch.tv/videos/{vod_id}"], timeout=120)
    segs = hls_segments(out.strip().splitlines()[0], start, length)
    ts = Path(dest).with_suffix(".ts")
    with open(ts, "wb") as f:
        for _, url in segs:
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            f.write(r.content)
    offset = max(0.0, start - segs[0][0])
    run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{offset:.2f}", "-i", str(ts),
         "-t", str(length), "-c", "copy", "-bsf:a", "aac_adtstoasc", str(dest)], timeout=300)
    ts.unlink(missing_ok=True)
    if not Path(dest).exists() or Path(dest).stat().st_size < 100_000:
        raise RuntimeError("parça indirilemedi")


_WHISPER = None


def transcribe(path, model_name, lang):
    global _WHISPER
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        log("faster-whisper yok, altyazı atlanıyor")
        return []
    if _WHISPER is None:
        _WHISPER = WhisperModel(model_name, device="cpu", compute_type="int8")
    segs, _ = _WHISPER.transcribe(str(path), language=lang, word_timestamps=True, vad_filter=True)
    return [(w.start, w.end, w.word.strip()) for s in segs for w in (s.words or [])]


def mask(text):
    """Altyazıda ağır küfürleri yıldızla (platform kuralları + 'olumsuz gösterme' yasağı)."""
    bad = r"\b(n[i1]gg\w*|f[a4]gg?\w*|retard\w*)\b"
    return re.sub(bad, lambda m: m.group(0)[0] + "*" * (len(m.group(0)) - 1), text, flags=re.I)


def ass_time(t):
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def esc(t):
    t = re.sub(r"[^\u0000-\uFFFF]|[\u2600-\u27BF\uFE0F]", "", t)   # emojiler videoda kutu olarak çıkar
    return t.replace("{", "").replace("}", "").replace("\\", "").strip()


def write_ass(words, hook, path):
    head = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,Arial,72,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,3,18,0,8,70,70,190,1
Style: Sub,Arial,84,&H0000F0FF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,2,2,60,60,430,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(head)
        if hook:
            f.write(f"Dialogue: 1,0:00:00.00,9:00:00.00,Hook,,0,0,0,,{esc(hook)}\n")
        group = []
        for w in words + [None]:
            if w is not None:
                group.append(w)
            if group and (w is None or len(group) == 3 or group[-1][1] - group[0][0] > 1.2
                          or w[2].endswith((".", "?", "!"))):
                text = esc(mask(" ".join(x[2] for x in group))).upper()
                f.write(f"Dialogue: 0,{ass_time(group[0][0])},{ass_time(group[-1][1] + 0.05)},Sub,,0,0,0,,{text}\n")
                group = []


def prepare_logo(url, camp_id):
    """Resmi logo dosyasını indirip PNG'ye çevirir (bir kez)."""
    png = ASSETS / f"{camp_id}_logo.png"
    if png.exists():
        return png
    import requests
    from PIL import Image
    raw = ASSETS / f"{camp_id}_logo_raw"
    raw.write_bytes(requests.get(url, timeout=60).content)
    Image.open(raw).convert("RGBA").save(png)
    raw.unlink()
    return png


def render(src, dest, ass, logo=None, max_seconds=58):
    """
    1080x1920 düzen:
      üstte hook yazısı (siyah kutu) | ortada yayının TAMAMI (yayıncının kamerası kesilmez)
      altında resmi logo (sadece kampanya istiyorsa) | altta büyük sarı altyazı
      arka plan: aynı görüntünün bulanık hali
    """
    fc = ("[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
          "gblur=sigma=28,eq=brightness=-0.18[bg];"
          "[0:v]scale=1080:-2,setsar=1[fg];"
          "[bg][fg]overlay=(W-w)/2:560[v0]")
    inputs = ["-i", str(Path(src).resolve())]
    last = "v0"
    if logo:
        inputs += ["-loop", "1", "-i", str(Path(logo).resolve())]
        fc += ";[1:v]scale=760:-2,format=rgba[lg];[v0][lg]overlay=(W-w)/2:1195:shortest=1[v1]"
        last = "v1"
    fc += f";[{last}]ass={Path(ass).name}[out]"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", fc,
           "-map", "[out]", "-map", "0:a?", "-t", str(max_seconds),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-r", "30",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
           "-movflags", "+faststart", str(Path(dest).resolve())]
    run(cmd, cwd=str(Path(ass).parent))
    # çok büyük dosyayı küçült (yükleme kolaylığı)
    if Path(dest).stat().st_size > 48 * 1024 * 1024:
        small = Path(dest).with_suffix(".small.mp4")
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(dest), "-c:v", "libx264",
             "-crf", "27", "-preset", "veryfast", "-c:a", "copy", str(small)])
        small.replace(dest)


# ================================================================ Metinler + kural kontrolü
def hook_text(moment, words, camp):
    if moment.get("hook"):
        return moment["hook"][:70]
    if words:
        q = " ".join(w[2] for w in words[:9]).strip()
        if q and not any(b in q.lower() for b in BLOCK_WORDS):
            return f'{camp["name"]}: "{mask(q)}"'[:70]
    return f'{camp["name"]} plays GTA 6'


def build_texts(camp, hook):
    tags = " ".join(camp["hashtags"])
    name = camp["must_mention_name"]
    base = re.sub(r"[#@]", "", hook)
    if name.lower() not in base.lower():
        base = f"{name} - {base}"
    yt_title = f"{base} {tags}"
    if len(yt_title) > 100:
        yt_title = f"{base[:100 - len(tags) - 2].rstrip()}… {tags}"
    caption = " ".join(x for x in [base, camp.get("caption_text", ""), tags] if x)
    return yt_title, caption


def validate(camp, yt_title, caption, logo_used):
    """Kampanya kurallarından birine uymayan klip üretilmez."""
    errs = []
    for txt, where in ((yt_title, "başlık"), (caption, "açıklama")):
        if camp["must_mention_name"].lower() not in txt.lower():
            errs.append(f"{where}: yayıncı adı yok")
        for h in camp["hashtags"]:
            if h.lower() not in txt.lower():
                errs.append(f"{where}: {h} yok")
        if camp.get("forbid_links") and re.search(r"https?://|www\.|\.com|\.tv", txt, re.I):
            errs.append(f"{where}: link var (yasak)")
    if camp.get("caption_text") and camp["caption_text"] not in caption:
        errs.append("zorunlu açıklama cümlesi yok")
    if camp.get("logo_overlay") and not logo_used:
        errs.append("zorunlu logo eklenmedi")
    if not camp.get("allow_logos") and logo_used:
        errs.append("logo yasak ama eklendi")
    if len(yt_title) > 100:
        errs.append("başlık 100 karakteri aşıyor")
    return errs


# ================================================================ Ana akış
def load_state():
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"done": []}


def make_clip(camp, d, m):
    raw = TMP / f"{m['key']}_raw.mp4"
    log(f"   indiriliyor: yayın {m['vod']} @ {m['start']}s ({m['length']}s)")
    download_section(m["vod"], m["start"], m["length"], raw)
    log("   altyazı çıkarılıyor")
    words = transcribe(raw, d["whisper_model"], d["language"])
    log(f"   {len(words)} kelime, video hazırlanıyor")
    hook = hook_text(m, words, camp)
    m["hook"] = hook
    yt_title, caption = build_texts(camp, hook)
    logo = prepare_logo(camp["logo_overlay"], camp["id"]) if camp.get("logo_overlay") else None
    errs = validate(camp, yt_title, caption, bool(logo))
    if errs:
        raise RuntimeError("KURAL HATASI: " + "; ".join(errs))
    ass = TMP / f"{m['key']}.ass"
    write_ass(words, hook, ass)
    final = OUT / f"{m['key']}.mp4"
    render(raw, final, ass, logo, d["max_seconds"])
    raw.unlink(missing_ok=True)
    return final, yt_title, caption


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test-render")
    ap.add_argument("--campaign", default="kreekcraft")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "campaigns.yaml").read_text(encoding="utf-8"))
    d = cfg["defaults"]
    for p in (OUT, TMP, ASSETS):
        p.mkdir(exist_ok=True)

    if args.test_render:
        camp = next(c for c in cfg["campaigns"] if c["id"] == args.campaign)
        m = {"key": "test", "hook": f"{camp['name']} gets jumped by NPCs in GTA 6"}
        yt_title, caption = build_texts(camp, m["hook"])
        logo = None
        if camp.get("logo_overlay"):
            local = ASSETS / f"{camp['id']}_logo.png"
            logo = local if local.exists() else None
        print("Kural kontrolü:", validate(camp, yt_title, caption, bool(logo)) or "TEMİZ")
        print("Başlık:", yt_title, "\nAçıklama:", caption)
        words = [(0.3, 0.7, "No"), (0.7, 1.0, "way"), (1.0, 1.4, "bro!"), (2.0, 2.6, "RUN!")]
        write_ass(words, m["hook"], TMP / "test.ass")
        render(args.test_render, OUT / f"test_{camp['id']}.mp4", TMP / "test.ass", logo)
        log("Hazır:", OUT / f"test_{camp['id']}.mp4")
        return

    state = load_state()
    done = set(state["done"])
    total = 0
    manifest = []
    for camp in cfg["campaigns"]:
        if not camp.get("active"):
            continue
        if not campaign_open(camp):
            log(f"{camp['name']}: kampanya tarihi dışında, atlandı")
            continue
        try:
            vods = eligible_vods(camp, d)
            moments = moments_from_clips(camp, d, vods) if vods else []
        except Exception as e:
            log(f"{camp['name']}: Twitch verisi alınamadı: {e}")
            continue
        new = [m for m in moments if m["key"] not in done][: d["max_clips_per_run"]]
        log(f"{camp['name']}: {len(vods)} uygun yayın, {len(moments)} an, {len(new)} yeni")
        for m in new:
            if args.dry_run:
                log(f"   -> {m['hook']} | yayın {m['vod']} @ {m['start']}s, {m['length']}s, {m['views']} izlenme")
                continue
            try:
                path, yt_title, caption = make_clip(camp, d, m)
                manifest.append({"file": path.name, "campaign": camp["id"], "name": camp["name"],
                                 "youtube_title": yt_title, "caption": caption,
                                 "submit_url": camp["submit_url"],
                                 "submit_within_minutes": camp.get("submit_within_minutes"),
                                 "payout": camp["payout"], "hook": m["hook"]})
                log(f"   ✓ {yt_title}")
                total += 1
            except Exception as e:
                log(f"   ✗ {m['key']}: {type(e).__name__}: {e}")
                if "KURAL HATASI" not in str(e):
                    continue                  # geçici hata: sonraki turda tekrar dene
            done.add(m["key"])
            state["done"].append(m["key"])
    if not args.dry_run:
        state["done"] = state["done"][-3000:]
        STATE_FILE.write_text(json.dumps(state, indent=1))
        if manifest:
            # Hazır kliplerin başlık/açıklama/gönderim bilgileri (Releases sayfasına konur)
            (OUT / "klipler.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
    log(f"Bitti: {total} klip")


if __name__ == "__main__":
    main()
