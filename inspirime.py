#!/usr/bin/env python3
"""Inspirime - an inspiration library for screenshots saved from Pinterest/Dribbble.

  ./inspirime.py scan [paths...] [--source NAME]   index new images (copies into library/)
  ./inspirime.py build                             write gallery.html
  ./inspirime.py open                              build + open the gallery
  ./inspirime.py selfcheck                         assert-based check of the pure logic

Images are copied to library/, described by a vision model (free local router by default),
and classified by measured palette (Pillow) plus model-supplied style tags.
"""
import argparse
import base64
import colorsys
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent
INBOX, LIB, DB, OUT = ROOT / "inbox", ROOT / "library", ROOT / "inspirime.db", ROOT / "gallery.html"
EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}
VURL = os.environ.get("INSPIRIME_VISION_URL", "http://127.0.0.1:3001/v1")
VMODEL = os.environ.get("INSPIRIME_VISION_MODEL", "auto")
GMODEL = os.environ.get("INSPIRIME_GOOGLE_MODEL", "gemini-3.5-flash-lite")
PROMPT = (
    "You see one saved design screenshot from Pinterest or Dribbble. Reply with JSON only, no prose:\n"
    '{"type": exactly one of "landing|dashboard|mobile|poster|article|other", '
    '"style": up to 3 tags from [minimal, brutalist, editorial, swiss, retro, playful, glassmorphism, '
    'gradient, typographic, three-d, illustration, dark-mode, corporate, luxury], '
    '"mood": one word, "desc": one sentence in Swedish describing the visual idea}'
)
SCHEMA = """create table if not exists shots(
  key text primary key, file text not null, w int, h int,
  colors text, hue int, huefam text, source text,
  type text, style text, mood text, desc text, added text)"""


def env_key(*names, default=""):
    """Read one of names from the environment, else from ~/.hermes/.env (tolerantly)."""
    for n in names:
        if os.environ.get(n):
            return os.environ[n]
    dotenv = Path.home() / ".hermes" / ".env"
    if dotenv.exists():
        for n in names:
            m = re.search(rf"^{n}=(.+)$", dotenv.read_text(errors="ignore"), re.M)
            if m:
                return m.group(1).strip().strip("'\"")
    return default


def db():
    con = sqlite3.connect(DB)
    con.execute(SCHEMA)
    return con


def palette(im, n=5):
    q = im.convert("RGB").resize((100, 100)).quantize(n)
    pal = q.getpalette()
    return ["#%02x%02x%02x" % tuple(pal[i * 3:i * 3 + 3]) for _, i in sorted(q.getcolors(), reverse=True)]


def hue_of(hexcolor):
    r, g, b = (int(hexcolor[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h, _, _ = colorsys.rgb_to_hls(r, g, b)
    return int(h * 360)


def accent(colors):
    """The palette entry that actually carries colour: most saturated mid-lightness tone.
    Sorting on the dominant tone alone makes every white UI screen 'neutral'."""
    def score(c):
        r, g, b = (int(c[i:i + 2], 16) / 255 for i in (1, 3, 5))
        _, l, s = colorsys.rgb_to_hls(r, g, b)
        return s if 0.12 < l < 0.92 else 0
    return max(colors, key=score) if colors else "#000000"


def hue_family(hexcolor):
    r, g, b = (int(hexcolor[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    if s < 0.18 or l < 0.08 or l > 0.95:
        return "neutral"
    d = h * 360
    if d < 70 or d >= 320:
        return "warm"
    return "green" if d < 165 else "cool"


def parse_meta(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        d = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        d = {}
    tags = [str(t).strip().lower() for t in (d.get("style") or []) if str(t).strip()][:3]
    return {
        "type": (str(d.get("type") or "other").strip().lower() or "other")[:20],
        "style": tags,
        "mood": str(d.get("mood") or "")[:40],
        "desc": str(d.get("desc") or "")[:400],
    }


def gemini_meta(data, mime):
    """Primary path: Gemini direct (a few öre per image, reliable). None = unavailable."""
    key = env_key("INSPIRIME_GOOGLE_KEY", "GOOGLE_API_KEY")
    if not key:
        return None
    body = json.dumps({"contents": [{"parts": [{"text": PROMPT},
                                               {"inline_data": {"mime_type": mime, "data": data}}]}],
                       "generationConfig": {"maxOutputTokens": 600, "temperature": 0.2}}).encode()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GMODEL}:generateContent"
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=body, headers={
                "x-goog-api-key": key, "Content-Type": "application/json"})
            d = json.load(urllib.request.urlopen(req, timeout=120))
            return parse_meta("".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"]))
        except Exception:
            time.sleep(2 * (attempt + 1))
    return None


def router_meta(data, mime):
    """Fallback path: local free router (OpenAI-compatible). Rate limited, so it retries."""
    key = env_key("INSPIRIME_ROUTER_KEY", "FLA_KEY", "OMNIROUTE_API_KEY")
    body = json.dumps({"model": VMODEL, "max_tokens": 600, "messages": [{"role": "user", "content": [
        {"type": "text", "text": PROMPT},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}]}]}).encode()
    for attempt in range(3):
        req = urllib.request.Request(f"{VURL}/chat/completions", data=body, headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            out = json.load(urllib.request.urlopen(req, timeout=180))
            return parse_meta(out["choices"][0]["message"]["content"])
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="ignore")
            m = re.search(r"(?:reset|retry)[^0-9]*(\d+)s", msg)
            time.sleep(min(int(m.group(1)) + 1 if m else 3 * (attempt + 1), 65))
        except Exception:
            time.sleep(3)
    return None


def describe(path):
    """type/style/mood/desc for one image. None means 'try again next scan'."""
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    data = base64.b64encode(path.read_bytes()).decode()
    for call in (gemini_meta, router_meta):
        meta = call(data, mime)
        if meta and meta["desc"]:
            return meta
    return None


def source_of(p, explicit):
    if explicit:
        return explicit
    try:
        rel = p.resolve().relative_to(INBOX.resolve())
        return rel.parts[0] if len(rel.parts) > 1 else "okand"
    except ValueError:
        return "okand"


def scan(paths, source=None):
    con = db()
    files = [p for p in paths if p.suffix.lower() in EXT]
    # largest file first: an image saved as both .jpg and .webp indexes once, as the better copy
    files.sort(key=lambda p: p.stat().st_size, reverse=True)
    new = 0
    for p in files:
        try:
            im = Image.open(p)
            w, h = im.size
            colors = palette(im)
        except Exception as e:
            print(f"skip {p.name}: {e}")
            continue
        key = f"{p.stem}-{w}x{h}"
        if con.execute("select 1 from shots where key=?", (key,)).fetchone():
            continue
        LIB.mkdir(parents=True, exist_ok=True)
        dest, i = LIB / f"{p.stem}{p.suffix.lower()}", 2
        while dest.exists() and dest.stat().st_size != p.stat().st_size:
            dest, i = LIB / f"{p.stem}-{i}{p.suffix.lower()}", i + 1
        if not dest.exists():
            shutil.copy2(p, dest)
        meta = describe(dest) or {}
        hue_color = accent(colors)
        con.execute("insert or replace into shots(key,file,w,h,colors,hue,huefam,source,type,style,mood,desc,added)"
                    " values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (key, f"library/{dest.name}", w, h, json.dumps(colors), hue_of(hue_color),
                     hue_family(hue_color), source_of(p, source), meta.get("type"),
                     json.dumps(meta.get("style") or []), meta.get("mood"), meta.get("desc"),
                     datetime.now(timezone.utc).isoformat(timespec="seconds")))
        con.commit()
        new += 1
        print(f"+ {dest.name}  [{meta.get('type', 'pending')}] {meta.get('desc', '')}")
    for key, file in con.execute("select key,file from shots where desc is null or desc=''").fetchall():
        meta = describe(ROOT / file)
        if meta:
            con.execute("update shots set type=?,style=?,mood=?,desc=? where key=?",
                        (meta["type"], json.dumps(meta["style"]), meta["mood"], meta["desc"], key))
            con.commit()
            print(f"~ described {file}")
    total = con.execute("select count(*) from shots").fetchone()[0]
    pending = con.execute("select count(*) from shots where desc is null or desc=''").fetchone()[0]
    print(f"{new} new, {total} total, {pending} pending (run scan again later)")


TEMPLATE = r"""<!doctype html>
<html lang="sv"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Inspirime</title>
<style>
:root{--bg:#0e0f11;--card:#16181c;--line:#262a30;--fg:#e8eaed;--dim:#8b9199;--acc:#c8f169}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Inter",sans-serif}
header{position:sticky;top:0;z-index:9;background:rgba(14,15,17,.94);backdrop-filter:blur(14px);border-bottom:1px solid var(--line);padding:18px 26px 14px}
h1{margin:0 0 12px;font-size:13px;font-weight:600;letter-spacing:.22em;text-transform:uppercase}
h1 em{font-style:normal;color:var(--dim);letter-spacing:0;text-transform:none;font-weight:400;margin-left:12px}
.bar{display:flex;gap:8px;flex-wrap:wrap}
input,select{background:var(--card);border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:8px 11px;font:inherit;font-size:13px;outline:none}
input:focus,select:focus{border-color:#3d434c}
#q{flex:1;min-width:240px}
.chip{background:var(--card);border:1px solid var(--line);color:var(--dim);border-radius:999px;padding:4px 11px;font-size:12px;cursor:pointer;line-height:1.5}
.chip:hover{border-color:#3d434c;color:var(--fg)}
.chip[aria-pressed=true]{background:var(--acc);border-color:var(--acc);color:#101204}
.row{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:9px}
.lbl{font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:#5b6169;min-width:52px}
main{padding:22px 26px 90px;column-count:4;column-gap:15px}
@media(max-width:1500px){main{column-count:3}}
@media(max-width:1000px){main{column-count:2}}
@media(max-width:640px){main{column-count:1}}
figure{margin:0 0 15px;break-inside:avoid;background:var(--card);border:1px solid var(--line);border-radius:11px;overflow:hidden}
figure img{display:block;width:100%;height:auto;background:#0b0c0e}
figcaption{padding:10px 12px 12px;font-size:12px;color:var(--dim)}
figcaption b{display:block;color:var(--fg);font-weight:500;font-size:12.5px;line-height:1.4;margin-bottom:5px}
.tags{display:flex;gap:4px;flex-wrap:wrap;margin-top:8px;align-items:center}
.tag{font-size:10px;padding:1px 6px;border-radius:4px;background:#22262c;color:#aeb4bc}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:3px;vertical-align:-1px}
.empty{padding:70px 26px;color:var(--dim);font-size:15px;line-height:1.7}
code{background:var(--card);border:1px solid var(--line);padding:2px 6px;border-radius:5px;font-size:13px}
</style></head><body>
<header>
<h1>Inspirime<em id=count></em></h1>
<div class=bar>
  <input id=q placeholder="Sök i beskrivning, stil, typ, källa …">
  <select id=sort>
    <option value=new>Nyast först</option>
    <option value=hue>Sortera på färg</option>
    <option value=type>Sortera på typ</option>
  </select>
</div>
<div class=row><span class=lbl>stil</span><span class=row id=fstyle></span></div>
<div class=row><span class=lbl>typ</span><span class=row id=ftype></span></div>
<div class=row><span class=lbl>färg</span><span class=row id=fhue></span></div>
<div class=row><span class=lbl>källa</span><span class=row id=fsrc></span></div>
</header>
<main id=grid></main>
<script>
const DATA = /*DATA*/[];
const HUE = {warm:"varm", cool:"kall", green:"grön", neutral:"neutral"};
const st = {q:"", style:"", type:"", hue:null, source:""};
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

function chips(host, values, key, label) {
  const el = document.getElementById(host);
  el.innerHTML = "";
  values.forEach(v => {
    const b = document.createElement("button");
    b.className = "chip";
    b.textContent = label ? label(v) : v;
    b.setAttribute("aria-pressed", String(st[key] === v));
    b.onclick = () => { st[key] = st[key] === v ? "" : v; render(); };
    el.append(b);
  });
}

function render() {
  const q = st.q.toLowerCase();
  let rows = DATA.filter(d => {
    if (st.style && !(d.style || []).includes(st.style)) return false;
    if (st.type && d.type !== st.type) return false;
    if (st.hue && d.huefam !== st.hue) return false;
    if (st.source && d.source !== st.source) return false;
    return !q || [d.desc, d.mood, d.type, d.source, (d.style || []).join(" ")].join(" ").toLowerCase().includes(q);
  });
  const sort = document.getElementById("sort").value;
  rows.sort((a, b) => sort === "hue" ? (a.hue - b.hue) || a.key.localeCompare(b.key)
    : sort === "type" ? (a.type || "").localeCompare(b.type || "") || a.key.localeCompare(b.key)
    : b.added.localeCompare(a.added));

  chips("fstyle", [...new Set(DATA.flatMap(d => d.style || []))].sort(), "style");
  chips("ftype", [...new Set(DATA.map(d => d.type).filter(Boolean))].sort(), "type");
  chips("fhue", ["warm", "cool", "green", "neutral"], "hue", v => HUE[v]);
  chips("fsrc", [...new Set(DATA.map(d => d.source).filter(Boolean))].sort(), "source");
  document.getElementById("count").textContent =
    rows.length + " av " + DATA.length + " bilder";

  document.getElementById("grid").innerHTML = rows.length ? rows.map(d => `
<figure>
  <img loading=lazy src="${esc(d.file)}" alt="${esc(d.desc || d.key)}">
  <figcaption>
    <b>${esc(d.desc || "(väntar på beskrivning - kör scan igen)")}</b>
    ${esc(d.type || "")}${d.mood ? " · " + esc(d.mood) : ""}${d.w ? " · " + d.w + "×" + d.h : ""}
    <div class=tags>${d.colors.map(c => `<span class=dot style="background:${esc(c)}" title="${esc(c)}"></span>`).join("")}
      ${(d.style || []).map(t => `<span class=tag>${esc(t)}</span>`).join("")}</div>
  </figcaption>
</figure>`).join("") : `<div class=empty>Inga träffar.<br>Lägg skärmdumpar i <code>inbox/pinterest</code> eller <code>inbox/dribbble</code> och kör <code>inspirime scan</code>.</div>`;
}

document.getElementById("q").oninput = e => { st.q = e.target.value; render(); };
document.getElementById("sort").onchange = render;
render();
</script></body></html>
"""


def build():
    con = db()
    rows = [dict(zip(("key", "file", "w", "h", "colors", "hue", "huefam", "source",
                      "type", "style", "mood", "desc", "added"), r))
            for r in con.execute("select key,file,w,h,colors,hue,huefam,source,type,style,mood,desc,added"
                                 " from shots order by added desc")]
    for r in rows:
        r["colors"] = json.loads(r["colors"] or "[]")
        r["style"] = json.loads(r["style"] or "[]")
        # the colour classifier may improve between scans: refresh it from the stored palette
        hue_color = accent(r["colors"])
        r["hue"], r["huefam"] = hue_of(hue_color), hue_family(hue_color)
        con.execute("update shots set hue=?,huefam=? where key=?", (r["hue"], r["huefam"], r["key"]))
    con.commit()
    OUT.write_text(TEMPLATE.replace("/*DATA*/[]", json.dumps(rows, ensure_ascii=False)))
    print(f"{len(rows)} shots -> {OUT}")


def selfcheck():
    tmp = Path(os.environ.get("TMPDIR", "/tmp")) / "inspirime-selfcheck.png"
    Image.new("RGB", (400, 300), (198, 26, 40)).save(tmp)
    pal = palette(Image.open(tmp))
    assert 1 <= len(pal) <= 5 and pal[0].startswith("#"), pal
    assert hue_family(pal[0]) == "warm", (pal[0], hue_family(pal[0]))
    assert 350 <= hue_of(pal[0]) or hue_of(pal[0]) <= 5, hue_of(pal[0])
    assert hue_family("#1b1c20") == "neutral"
    assert accent(["#f0f1f2", "#fbfbfb", "#1d545a"]) == "#1d545a", "accent must skip near-white"
    meta = parse_meta('```json\n{"type":"Landing","style":["Minimal","editorial","minimal"],"mood":"calm","desc":"Ett lugnt gränssnitt."}\n```')
    assert meta["type"] == "landing" and meta["style"] == ["minimal", "editorial", "minimal"], meta
    assert parse_meta("no json here")["desc"] == "", parse_meta("nope")
    global DB
    DB, real = ROOT / "selfcheck.db", DB
    try:
        con = db()
        con.execute("insert or replace into shots(key,file,w,h,colors,hue,huefam,source,type,style,mood,desc,added)"
                    " values('a-400x300','library/a.png',400,300,'[]',0,'warm','pinterest','landing','[]','','x','t')")
        con.execute("insert or replace into shots(key,file,w,h,colors,hue,huefam,source,type,style,mood,desc,added)"
                    " values('a-400x300','library/a.webp',400,300,'[]',0,'warm','pinterest','landing','[]','','y','t')")
        con.commit()
        assert con.execute("select count(*) from shots").fetchone()[0] == 1
        con.close()
    finally:
        DB = real
        (ROOT / "selfcheck.db").unlink(missing_ok=True)
    tmp.unlink(missing_ok=True)
    print("selfcheck ok")


def main():
    ap = argparse.ArgumentParser(prog="inspirime")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="index new images")
    s.add_argument("paths", nargs="*", type=Path)
    s.add_argument("--source", help="source label (default: the inbox subfolder name)")
    sub.add_parser("build", help="write gallery.html")
    sub.add_parser("open", help="build and open gallery.html")
    sub.add_parser("selfcheck", help="run the assert-based check")
    a = ap.parse_args()

    if a.cmd == "scan":
        paths = a.paths or [INBOX]
        files = []
        for p in paths:
            if p.is_dir():
                files += [f for f in p.rglob("*") if f.is_file()]
            elif p.is_file():
                files.append(p)
            else:
                sys.exit(f"no such path: {p}")
        scan(files, a.source)
    elif a.cmd == "build":
        build()
    elif a.cmd == "open":
        build()
        subprocess.run(["xdg-open", str(OUT)], check=False)
    else:
        selfcheck()


if __name__ == "__main__":
    main()
