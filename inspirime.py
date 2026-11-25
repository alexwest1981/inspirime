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
import threading
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
    '"mood": one word, "desc": one sentence in Swedish about what the screen is and does, '
    '"layout": one dense line in Swedish on how the page is built (columns, sidebar widths, grid), '
    '"sections": every visible block in reading order, max 8, each 2-5 words in Swedish, '
    '"typography": one line in Swedish: serif/sans/mono, how many weights, label treatment, size feel, '
    '"density": "luftig"|"medel"|"tat", "radius": "0"|"4"|"8"|"12"|"16"|"pill", '
    '"imagery": one line in Swedish: none/photos/3d/illustration/icons only, '
    '"notes": one line in Swedish on shadows, borders, texture, motion hints - or empty string}'
)
META_FIELDS = ("type", "style", "mood", "desc", "layout", "sections", "typography",
               "density", "radius", "imagery", "notes")
SCHEMA = """create table if not exists shots(
  key text primary key, file text not null, w int, h int,
  colors text, hue int, huefam text, source text,
  type text, style text, mood text, desc text, added text,
  layout text, sections text, typography text, density text, radius text,
  imagery text, notes text)"""


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
    # older indexes predate the structured fields: add what is missing instead of rebuilding
    for col in META_FIELDS[4:] + ("w", "h", "colors", "hue", "huefam", "source"):
        try:
            con.execute(f"alter table shots add column {col} text")
        except sqlite3.OperationalError:
            pass
    return con


def palette(im, n=12):
    """Measured palette: hex plus how much of the picture it covers, largest first.
    n=12 rather than 5: an accent used on 2 % of a white screen vanishes at 5 colours."""
    q = im.convert("RGB").resize((100, 100)).quantize(n)
    pal = q.getpalette()
    counted = sorted(q.getcolors(), reverse=True)
    total = sum(c for c, _ in counted) or 1
    return [{"hex": "#%02x%02x%02x" % tuple(pal[i * 3:i * 3 + 3]), "pct": round(100 * c / total)}
            for c, i in counted]


def hexes(colors):
    """Palette entries are dicts now; older rows stored plain hex strings."""
    return [c["hex"] if isinstance(c, dict) else c for c in colors or []]


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


def contrast(a, b):
    """WCAG contrast ratio between two hex colours."""
    def rel(h):
        def lin(v):
            v /= 255
            return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
        r, g, bl = (int(h[i:i + 2], 16) for i in (1, 3, 5))
        return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(bl)
    lo, hi = sorted((rel(a), rel(b)))
    return round((hi + 0.05) / (lo + 0.05), 2)


def analyse(colors):
    """Background, real accent and real text colour from the measured palette.
    accent/text are None when the picture genuinely does not contain them - better than inventing one."""
    hx = hexes(colors)
    if not hx:
        return {"bg": "#ffffff", "accent": None, "text": None, "dark": False}
    bg = hx[0]
    dark = luma(bg) < 0.4

    def chroma(c):
        r, g, b = (int(c[i:i + 2], 16) / 255 for i in (1, 3, 5))
        _, l, s = colorsys.rgb_to_hls(r, g, b)
        return s if 0.12 < l < 0.92 else 0

    acc = max(hx, key=chroma)
    acc = acc if chroma(acc) >= 0.25 else None
    txt = max(hx, key=luma) if dark else min(hx, key=luma)
    txt = txt if contrast(txt, bg) >= 4.5 else None
    return {"bg": bg, "accent": acc, "text": txt, "dark": dark}


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

    def tags(v, limit):
        if isinstance(v, str):
            v = [t for t in re.split(r"[,;]", v)]
        return [str(t).strip().lower() for t in (v or []) if str(t).strip()][:limit]

    def line(v, limit):
        return " ".join(str(v or "").split())[:limit]

    return {
        "type": (line(d.get("type"), 20).lower() or "other"),
        "style": tags(d.get("style"), 3),
        "mood": line(d.get("mood"), 40),
        "desc": line(d.get("desc"), 400),
        "layout": line(d.get("layout"), 300),
        "sections": tags(d.get("sections"), 8),
        "typography": line(d.get("typography"), 240),
        "density": line(d.get("density"), 20).lower(),
        "radius": line(d.get("radius"), 20).lower(),
        "imagery": line(d.get("imagery"), 200),
        "notes": line(d.get("notes"), 240),
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
    """Index the given image files. Returns the log lines so a caller can show them."""
    con = db()
    files = [p for p in paths if p.suffix.lower() in EXT]
    # largest file first: an image saved as both .jpg and .webp indexes once, as the better copy
    files.sort(key=lambda p: p.stat().st_size, reverse=True)
    out, new = [], 0
    for p in files:
        try:
            im = Image.open(p)
            w, h = im.size
            colors = palette(im)
        except Exception as e:
            out.append(f"skip {p.name}: {e}")
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
        a = analyse(colors)
        hue_color = a["accent"] or a["bg"]
        row = {"key": key, "file": f"library/{dest.name}", "w": w, "h": h,
               "colors": json.dumps(colors), "hue": hue_of(hue_color), "huefam": hue_family(hue_color),
               "source": source_of(p, source),
               "added": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               **{k: json.dumps(v) if isinstance(v, list) else v for k, v in meta.items()}}
        con.execute(f"insert or replace into shots({','.join(row)})"
                    f" values({','.join('?' * len(row))})", tuple(row.values()))
        con.commit()
        new += 1
        out.append(f"+ {dest.name}  [{meta.get('type', 'pending')}] {meta.get('desc', '')}")
    for key, file in con.execute("select key,file from shots where desc is null or desc=''").fetchall():
        meta = describe(ROOT / file)
        if meta:
            values = {k: json.dumps(v) if isinstance(v, list) else v for k, v in meta.items()}
            con.execute(f"update shots set {','.join(k + '=?' for k in values)} where key=?",
                        (*values.values(), key))
            con.commit()
            out.append(f"~ described {file}")
    total = con.execute("select count(*) from shots").fetchone()[0]
    pending = con.execute("select count(*) from shots where desc is null or desc=''").fetchone()[0]
    out.append(f"{new} new, {total} total, {pending} pending (run scan again later)")
    con.close()
    return out


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
figure{margin:0 0 15px;break-inside:avoid;background:var(--card);border:1px solid var(--line);border-radius:11px;overflow:hidden;cursor:pointer}
figure:hover{border-color:#3d434c}
figure img{display:block;width:100%;height:auto;background:#0b0c0e}
figcaption{padding:10px 12px 12px;font-size:12px;color:var(--dim)}
figcaption b{display:block;color:var(--fg);font-weight:500;font-size:12.5px;line-height:1.4;margin-bottom:5px}
.tags{display:flex;gap:4px;flex-wrap:wrap;margin-top:8px;align-items:center}
.tag{font-size:10px;padding:1px 6px;border-radius:4px;background:#22262c;color:#aeb4bc}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:3px;vertical-align:-1px}
.empty{padding:70px 26px;color:var(--dim);font-size:15px;line-height:1.7}
code{background:var(--card);border:1px solid var(--line);padding:2px 6px;border-radius:5px;font-size:13px}
#scanbtn{border-color:#3d434c;color:var(--fg);white-space:nowrap}
#scanbtn:disabled{opacity:.55;cursor:progress}
#scanmsg{font-size:12px;color:var(--dim)}
dialog{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:14px;padding:20px 22px;width:min(920px,94vw)}
dialog::backdrop{background:rgba(5,6,8,.72)}
dialog h2{margin:0 0 12px;font-size:13px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--dim)}
#ptext{width:100%;background:#0f1114;border:1px solid var(--line);border-radius:9px;color:var(--fg);font:12.5px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;padding:12px;resize:vertical}
.dlgbar{display:flex;gap:8px;justify-content:flex-end;margin-top:12px}
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
  <button id=scanbtn class=chip title="Leta nya bilder i inbox/">Scanna nya</button>
  <span id=scanmsg></span>
</div>
<div class=row><span class=lbl>stil</span><span class=row id=fstyle></span></div>
<div class=row><span class=lbl>typ</span><span class=row id=ftype></span></div>
<div class=row><span class=lbl>färg</span><span class=row id=fhue></span></div>
<div class=row><span class=lbl>källa</span><span class=row id=fsrc></span></div>
</header>
<main id=grid></main>
<dialog id=promptdlg>
  <h2 id=pdlgtitle>Promp</h2>
  <textarea id=ptext rows=24 spellcheck=false></textarea>
  <div class=dlgbar>
    <button id=pclose class=chip>Stäng</button>
    <button id=pcopy class=chip>Kopiera</button>
  </div>
</dialog>
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
<figure data-key="${esc(d.key)}" title="Klicka för att skapa en prompt">
  <img loading=lazy src="${esc(d.file)}" alt="${esc(d.desc || d.key)}">
  <figcaption>
    <b>${esc(d.desc || "(väntar på beskrivning - kör scan igen)")}</b>
    ${esc(d.type || "")}${d.mood ? " · " + esc(d.mood) : ""}${d.w ? " · " + d.w + "×" + d.h : ""}
    <div class=tags>${d.colors.map(c => `<span class=dot style="background:${esc(c.hex)}" title="${esc(c.hex)} ${esc(c.pct)} %"></span>`).join("")}
      ${(d.style || []).map(t => `<span class=tag>${esc(t)}</span>`).join("")}</div>
  </figcaption>
</figure>`).join("") : `<div class=empty>Inga träffar.<br>Lägg skärmdumpar i <code>inbox/pinterest</code> eller <code>inbox/dribbble</code> och tryck <b>Scanna nya</b>.</div>`;
}

const dlg = document.getElementById("promptdlg");
document.getElementById("grid").onclick = e => {
  const fig = e.target.closest("figure");
  const d = fig && DATA.find(x => x.key === fig.dataset.key);
  if (!d) return;
  document.getElementById("pdlgtitle").textContent =
    "prompt · " + (d.type || "inspiration") + ((d.style || []).length ? " · " + d.style.join(", ") : "");
  document.getElementById("ptext").value = d.prompt;
  document.getElementById("ptext").scrollTop = 0;
  dlg.showModal();
};
document.getElementById("pclose").onclick = () => dlg.close();
document.getElementById("pcopy").onclick = async () => {
  const t = document.getElementById("ptext"), b = document.getElementById("pcopy");
  try { await navigator.clipboard.writeText(t.value); }
  catch { t.select(); document.execCommand("copy"); }
  b.textContent = "Kopierat";
  setTimeout(() => { b.textContent = "Kopiera"; }, 1500);
};

const scanbtn = document.getElementById("scanbtn"), scanmsg = document.getElementById("scanmsg");
const lastLine = s => (s || "").trim().split("\n").pop() || "";
scanbtn.onclick = async () => {
  if (location.protocol === "file:") { scanmsg.textContent = "starta med: inspirime open"; return; }
  scanbtn.disabled = true;
  scanmsg.textContent = "scannar …";
  try {
    const r = await fetch("/scan", { method: "POST" });
    if (!r.ok) throw new Error(r.status);
    for (;;) {
      const s = await (await fetch("/scan/status")).json();
      scanmsg.textContent = s.running ? "scannar … " + lastLine(s.log) : lastLine(s.log);
      if (!s.running) { setTimeout(() => location.reload(), 800); return; }
      await new Promise(res => setTimeout(res, 2000));
    }
  } catch (e) {
    scanmsg.textContent = "ingen server - kör inspirime open";
    scanbtn.disabled = false;
  }
};

document.getElementById("q").oninput = e => { st.q = e.target.value; render(); };
document.getElementById("sort").onchange = render;
render();
</script></body></html>
"""


def luma(hexcolor):
    r, g, b = (int(hexcolor[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def hsl(hexcolor):
    r, g, b = (int(hexcolor[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    return int(h * 360), round(s * 100), round(l * 100)


def ratio(w, h):
    from math import gcd
    g = gcd(w, h) or 1
    a, b = w // g, h // g
    return f"{a}:{b}" if a <= 32 and b <= 32 else f"{w / h:.2f}:1"


DENSITY_HINT = {
    "luftig": "luftig = 28-36 px mellan sektioner, 18-24 px inne i kort, gott om luft runt text",
    "medel": "medel = 18-26 px mellan sektioner, 12-18 px inne i kort",
    "tat": "tät = 10-16 px mellan sektioner, 8-12 px inne i kort, små radavstånd",
}


def prompt_for(r):
    """A brief dense enough that a design agent has no room to interpret it wrong."""
    colors = r["colors"] or []
    a = analyse(colors)
    bg, acc, text, dark = a["bg"], a["accent"], a["text"], a["dark"]
    radius = r.get("radius") or ""
    rad = "999px (pill)" if radius == "pill" else (f"{radius} px" if radius.isdigit() else "oklart")
    orient = "liggande" if r["w"] > r["h"] else "stående" if r["h"] > r["w"] else "kvadrat"
    sections = r["sections"] or []
    style = r["style"] or []

    p = ["# MÅL",
         f"Bygg en {r['type'] or 'sida'} som ser ut att höra till samma produkt som referensen: "
         "samma uppbyggnad, samma färgvärden, samma typografiska hierarki, samma täthet. "
         "Eget innehåll och eget namn — formen ska vara omöjlig att ta miste på.",
         "",
         "# REFERENS"]
    if r.get("desc"):
        p.append(f"- Föreställer: {r['desc']}")
    p.append(f"- Fil: {ROOT / r['file']}")
    p.append(f"- Storlek: {r['w']}×{r['h']} px, {orient}, {ratio(r['w'], r['h'])}, källa: {r.get('source') or 'okänd'}")
    if r.get("layout"):
        p.append(f"- Uppbyggnad: {r['layout']}")
    if sections:
        p.append(f"- Sektioner i läsordning: {' → '.join(sections)}")
    if r.get("typography"):
        p.append(f"- Typografi: {r['typography']}")
    p.append(f"- Täthet: {r.get('density') or 'oklart'} · Hörnradie: {rad} · Bildspråk: {r.get('imagery') or 'oklart'}")
    if r.get("notes"):
        p.append(f"- Detaljer: {r['notes']}")

    p += ["", "# FÄRG — mätta på referensens pixlar, använd exakt dessa värden"]
    for i, c in enumerate(colors):
        share = f"{c['pct']} %" if isinstance(c, dict) and c.get("pct") else "andel okänd"
        note = "  ← största ytan (bakgrund)" if i == 0 else ""
        p.append(f"- {share} {hexes([c])[0]}{note}")
    if acc:
        ah, asat, al = hsl(acc)
        p.append(f"- Accent: {acc} (hue {ah}°, mättnad {asat} %, ljushet {al} %) — den enda accentfärgen")
    else:
        p.append("- Accent: ingen. Referensen är i praktiken monokrom — hitta INTE på en accentfärg, "
                 "använd bara ytorna ovan plus svart/vitt för text.")
    if text:
        p.append(f"- Textfärg: {text} mot bakgrunden {bg} (kontrast minst 4.5:1)")
    else:
        p.append(f"- Textfärg: inte mätbar i referensen. Använd en neutral mot {bg} och räkna på 4.5:1.")
    p.append(f"- Tema: {'mörkt' if dark else 'ljust'} (bakgrundens ljushet {round(luma(bg), 2)})")
    p.append(f"- Kulörfamilj: {r.get('huefam') or hue_family(acc or bg)}")

    p += ["", "# KRAV"]
    p.append("1. Använd hex-värdena ovan exakt. Inga egna nyanser, inga fler accenter, "
             "inga gradienter om inte 'gradient' står bland stilorden.")
    if r.get("layout"):
        p.append(f"2. Samma uppbyggnad: {r['layout']}")
    if sections:
        p.append(f"3. Samma block i samma ordning: {', '.join(sections)} — inga block tillkommer eller tas bort.")
    p.append(f"4. Hörnradie {rad} på varje kort, knapp och inmatningsfält. Samma överallt, inga undantag.")
    p.append(f"5. Täthet: {DENSITY_HINT.get((r.get('density') or '').lower(), 'håll samma luft som referensen')}.")
    if r.get("typography"):
        p.append(f"6. Typografi: {r['typography']} — max två teckensnitt, högst tre vikter, "
                 "radhöjd 1.4-1.6 för brödtext.")
    p.append(f"7. Huvudvyn i {r['w']}×{r['h']} px ({ratio(r['w'], r['h'])}); responsiv ner till 390 px "
             "utan att hierarkin eller sektionsordningen ändras.")
    if style:
        p.append(f"8. Stilorden som ska kännas igen: {', '.join(style)}.")
    p.append("9. All text på svenska, eget påhittat innehåll med rimliga siffror. Inga platshållare som "
             "'Lorem ipsum' eller 'Text här'.")

    p += ["", "# FÖRBJUDET",
          "- Kopiera inte text, logotyper, produktnamn eller bilder från referensen.",
          "- Inga externa resurser: inga webbfonter, inga stockbilder, inga ikonbibliotek från nätet.",
          "- Inga extra färger, skuggor eller animationer som inte syns i referensen."]

    p += ["", "# LEVERANS",
          "- En enda HTML-fil med CSS i <style> — inga byggsteg, inga dependencies.",
          "- Alla färger som CSS-variabler i :root med namnen --bg, --surface, --accent, --text.",
          "- Kommentera inget i koden; strukturen ska gå att läsa direkt."]

    p += ["", "# KONTROLL INNAN DU ÄR KLAR",
          f"- [ ] Bakgrund {bg}{', accent ' + acc if acc else ''}{' och text ' + text if text else ''} används exakt.",
          f"- [ ] Radie {rad} på alla ytor.",
          f"- [ ] Alla {len(sections) or 'nämnda'} sektioner finns i rätt ordning.",
          "- [ ] Ingen text eller bild hämtad från referensen.",
          "- [ ] Sidan fungerar i 390 px bredd utan horisontell scroll."]
    return "\n".join(p)


def build():
    con = db()
    cur = con.execute("select * from shots order by added desc")
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur]
    for r in rows:
        r["colors"] = json.loads(r["colors"] or "[]")
        # re-measure when the palette predates percentages or the wider 12-colour sample
        if not r["colors"] or isinstance(r["colors"][0], str) or len(r["colors"]) < 12:
            try:
                r["colors"] = palette(Image.open(ROOT / r["file"]))
                con.execute("update shots set colors=? where key=?", (json.dumps(r["colors"]), r["key"]))
            except Exception:
                r["colors"] = [{"hex": c, "pct": 0} if isinstance(c, str) else c for c in r["colors"]]
        r["style"] = json.loads(r["style"] or "[]")
        r["sections"] = json.loads(r["sections"] or "[]")
        # the colour classifier may improve between scans: refresh it from the stored palette
        a = analyse(r["colors"])
        hue_color = a["accent"] or a["bg"]
        r["hue"], r["huefam"] = hue_of(hue_color), hue_family(hue_color)
        r["prompt"] = prompt_for(r)
        con.execute("update shots set hue=?,huefam=? where key=?", (r["hue"], r["huefam"], r["key"]))
    con.commit()
    con.close()
    OUT.write_text(TEMPLATE.replace("/*DATA*/[]", json.dumps(rows, ensure_ascii=False)))
    print(f"{len(rows)} shots -> {OUT}")


def selfcheck():
    tmp = Path(os.environ.get("TMPDIR", "/tmp")) / "inspirime-selfcheck.png"
    Image.new("RGB", (400, 300), (198, 26, 40)).save(tmp)
    pal = palette(Image.open(tmp))
    assert 1 <= len(pal) <= 12 and pal[0]["hex"].startswith("#"), pal
    assert abs(sum(c["pct"] for c in pal) - 100) <= 2, pal
    assert hue_family(hexes(pal)[0]) == "warm", (pal, hue_family(hexes(pal)[0]))
    assert 350 <= hue_of(hexes(pal)[0]) or hue_of(hexes(pal)[0]) <= 5, hue_of(hexes(pal)[0])
    # a real accent on 2 % of a light screen must survive the palette measurement
    light = [{"hex": h, "pct": 10} for h in ("#fbfbfb", "#f3f3f4", "#a8adb6", "#89b2e4", "#e7e2ce")]
    assert analyse(light)["accent"] == "#89b2e4", analyse(light)
    mono = [{"hex": h, "pct": 20} for h in ("#111111", "#1a1a1a", "#333333", "#545455")]
    assert analyse(mono)["accent"] is None, analyse(mono)
    assert analyse(light)["text"] is None, analyse(light)
    assert analyse([{"hex": "#111111", "pct": 60}, {"hex": "#f0f1f2", "pct": 20}])["text"] == "#f0f1f2"
    assert contrast("#111111", "#fbfbfb") > 18 and contrast("#fbfbfb", "#fbfbfb") == 1.0
    assert hue_family("#1b1c20") == "neutral"
    assert accent(["#f0f1f2", "#fbfbfb", "#1d545a"]) == "#1d545a", "accent must skip near-white"
    meta = parse_meta('```json\n{"type":"Landing","style":["Minimal","editorial","minimal"],"mood":"calm",'
                      '"desc":"Ett lugnt gränssnitt.","layout":"vänsterspalt 240 px + 3 kolumner",'
                      '"sections":["topprad","KPI-kort","tabell"],"typography":"sans, två vikter",'
                      '"density":"tat","radius":"12","imagery":"ikoner","notes":"mjuka skuggor"}\n```')
    assert meta["type"] == "landing" and meta["style"] == ["minimal", "editorial", "minimal"], meta
    assert meta["sections"] == ["topprad", "kpi-kort", "tabell"] and meta["radius"] == "12", meta
    assert parse_meta("no json here")["desc"] == "", parse_meta("nope")
    brief = prompt_for({"type": "landing", "desc": "Ett lugnt gränssnitt.", "style": ["minimal"],
                        "mood": "calm", "colors": [{"hex": "#111111", "pct": 40}, {"hex": "#eeeeee", "pct": 30},
                                                   {"hex": "#2e8081", "pct": 10}],
                        "w": 1440, "h": 900, "file": "library/x.png", "source": "pinterest",
                        "layout": "vänsterspalt 240 px + kortgrid", "sections": ["topprad", "KPI-kort"],
                        "typography": "sans, två vikter", "density": "medel", "radius": "12",
                        "imagery": "ikoner", "notes": "mjuka skuggor", "huefam": "cool"})
    for needle in ("# MÅL", "# FÄRG", "# KRAV", "# FÖRBJUDET", "# LEVERANS", "40 %", "#2e8081",
                   "1440×900", "8:5", "topprad → KPI-kort", "12 px", "18-26 px", str(ROOT / "library/x.png")):
        assert needle in brief, (needle, brief)
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


SCAN = {"running": False, "log": ""}


def run_scan():
    """Scan the inbox in the background so the button does not hang on a slow model."""
    if SCAN["running"]:
        return False
    SCAN.update(running=True, log="startar")

    def work():
        try:
            lines = scan([f for f in INBOX.rglob("*") if f.is_file()] if INBOX.exists() else [])
            SCAN["log"] = "\n".join(lines)
        except Exception as e:
            SCAN["log"] = f"fel: {e}"
        finally:
            SCAN["running"] = False
            build()
    threading.Thread(target=work, daemon=True).start()
    return True


def serve(port=8789, open_browser=True):
    # local-only (127.0.0.1): serves the gallery plus /scan so the page can index new drops
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(ROOT), **kw)

        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.split("?")[0] == "/scan/status":
                return self._json(SCAN)
            super().do_GET()

        def do_POST(self):
            if self.path.split("?")[0] == "/scan":
                return self._json({"started": run_scan(), **SCAN})
            self.send_error(404)

        def _json(self, obj):
            body = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    build()
    url = f"http://127.0.0.1:{port}/gallery.html"
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        print(f"servern kör redan: {url}")
        if open_browser:
            subprocess.run(["xdg-open", url], check=False)
        return
    print(f"Inspirime: {url}  (Ctrl+C stänger)")
    if open_browser:
        threading.Timer(0.4, lambda: subprocess.run(["xdg-open", url], check=False)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()


def main():
    ap = argparse.ArgumentParser(prog="inspirime")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="index new images")
    s.add_argument("paths", nargs="*", type=Path)
    s.add_argument("--source", help="source label (default: the inbox subfolder name)")
    sub.add_parser("build", help="write gallery.html")
    o = sub.add_parser("open", help="serve the gallery and open it (scan button works here)")
    o.add_argument("--port", type=int, default=8789)
    o.add_argument("--no-browser", action="store_true")
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
        for line in scan(files, a.source):
            print(line)
    elif a.cmd == "build":
        build()
    elif a.cmd == "open":
        serve(a.port, not a.no_browser)
    else:
        selfcheck()


if __name__ == "__main__":
    main()
