#!/usr/bin/env python3
"""Inspirime - turn saved design screenshots into a searchable library your agent can read.

  inspirime demo                        create+index a small sample library (no API key needed)
  inspirime open                        serve the gallery on http://127.0.0.1:8789 and open it
  inspirime scan [paths...] [--source S] index new screenshots (copies into library/)
  inspirime brief KEY [--json]          print the design brief for one reference
  inspirime export [--out FILE]         dump the whole index as JSON
  inspirime tag KEY +minimal -corporate add/remove style tags by hand
  inspirime delete KEY                  remove one reference from the library
  inspirime build                       regenerate gallery.html only
  inspirime mcp                         stdio MCP server (handoffs/search/get/ack)
  inspirime selfcheck                   assert-based check, no network

Colour is measured from the pixels with Pillow; type/style/brief come from a vision model
(Gemini or any OpenAI-compatible endpoint). Without a key it still indexes and measures -
it just has no descriptions until you add one.
"""
import argparse
import base64
import colorsys
import json
import math
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

from PIL import Image, ImageDraw

__version__ = "0.2.0"


def home():
    """Where the library lives: $INSPIRIME_HOME, else the script's own folder when a library is
    already there (running from a checkout), else the XDG data dir so an installed copy never
    writes into site-packages."""
    env = os.environ.get("INSPIRIME_HOME")
    if env:
        return Path(env).expanduser().resolve()
    here = Path(__file__).resolve().parent
    if (here / "inspirime.db").exists() or (here / "inbox").is_dir():
        return here
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / "inspirime"


ROOT = home()
INBOX, LIB, DB, OUT = ROOT / "inbox", ROOT / "library", ROOT / "inspirime.db", ROOT / "gallery.html"
EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}
GOOGLE_MODELS = ["gemini-flash-latest", "gemini-2.5-flash", "gemini-3.5-flash-lite"]
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"


def router_url():
    """An extra OpenAI-compatible endpoint, opt-in via INSPIRIME_VISION_URL."""
    return os.environ.get("INSPIRIME_VISION_URL", "").rstrip("/")


def vision_model(default):
    return os.environ.get("INSPIRIME_VISION_MODEL") or default

PROMPT = (
    "You see one saved design screenshot from Pinterest or Dribbble. Reply with JSON only, no prose:\n"
    '{"type": exactly one of "landing|dashboard|mobile|poster|article|other", '
    '"style": up to 3 tags from [minimal, brutalist, editorial, swiss, retro, playful, glassmorphism, '
    'gradient, typographic, three-d, illustration, dark-mode, corporate, luxury], '
    '"mood": one word, "desc": one sentence in {language} about what the screen is and does, '
    '"layout": one dense line in {language} on how the page is built (columns, sidebar widths, grid), '
    '"sections": every visible block in reading order, max 8, each 2-5 words in {language}, '
    '"typography": one line in {language}: serif/sans/mono, how many weights, label treatment, size feel, '
    '"density": "luftig"|"medel"|"tat", "radius": "0"|"4"|"8"|"12"|"16"|"pill", '
    '"imagery": one line in {language}: none/photos/3d/illustration/icons only, '
    '"notes": one line in {language} on shadows, borders, texture, motion hints - or empty string}'
)


def vision_prompt():
    """The model writes descriptions in the same language as the brief, so nothing is mixed."""
    return PROMPT.replace("{language}", "Swedish" if _lang() == "sv" else "English")
META_FIELDS = ("type", "style", "mood", "desc", "layout", "sections", "typography",
               "density", "radius", "imagery", "notes")
SCHEMA = """create table if not exists shots(
  key text primary key, file text not null, w int, h int,
  colors text, hue int, huefam text, source text,
  type text, style text, mood text, desc text, added text,
  layout text, sections text, typography text, density text, radius text,
  imagery text, notes text)"""
HANDOFFS = """create table if not exists handoffs(
  id integer primary key, key text not null, target text, prompt text not null,
  created text, acked text)"""


def env_key(*names, default=""):
    """First of names present in the environment, then a .env next to the library or in the CWD."""
    for n in names:
        if os.environ.get(n):
            return os.environ[n]
    candidates = [Path.cwd() / ".env", ROOT / ".env", Path.home() / ".hermes" / ".env"]
    for dotenv in candidates:
        if not dotenv.exists():
            continue
        text = dotenv.read_text(errors="ignore")
        for n in names:
            m = re.search(rf"^{n}=(.+)$", text, re.M)
            if m:
                return m.group(1).strip().strip("'\"")
    return default


def db():
    con = sqlite3.connect(DB)
    con.execute(SCHEMA)
    con.execute(HANDOFFS)
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
    """Google direct. Walks the model aliases so a retired name or an overloaded model does not
    end the run; a real error (bad key, bad request) stops immediately instead of retrying."""
    key = env_key("GEMINI_API_KEY", "GOOGLE_API_KEY", "INSPIRIME_GOOGLE_KEY")
    if not key:
        return None
    body = json.dumps({"contents": [{"parts": [{"text": vision_prompt()},
                                               {"inline_data": {"mime_type": mime, "data": data}}]}],
                       "generationConfig": {"maxOutputTokens": 900, "temperature": 0.2}}).encode()
    models = [os.environ["INSPIRIME_GOOGLE_MODEL"]] if os.environ.get("INSPIRIME_GOOGLE_MODEL") else GOOGLE_MODELS
    for model in models:
        try:
            req = urllib.request.Request(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                data=body, headers={"x-goog-api-key": key, "Content-Type": "application/json"})
            d = json.load(urllib.request.urlopen(req, timeout=120))
            return parse_meta("".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"]))
        except urllib.error.HTTPError as e:
            if e.code not in (404, 429, 503):  # not "model gone / busy" - stop, do not hammer
                return None
        except Exception:
            return None
    return None


def chat_meta(url, key, model, data, mime):
    """Any OpenAI-compatible /chat/completions endpoint, with the image inline."""
    body = json.dumps({"model": model, "max_tokens": 900, "messages": [{"role": "user", "content": [
        {"type": "text", "text": vision_prompt()},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}]}]}).encode()
    for attempt in range(2):
        try:
            req = urllib.request.Request(f"{url}/chat/completions", data=body, headers={
                "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            out = json.load(urllib.request.urlopen(req, timeout=180))
            return parse_meta(out["choices"][0]["message"]["content"])
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="ignore")
            m = re.search(r"reset[^0-9]*(\d+)s", msg)
            if m and attempt == 0:  # rate limited: wait what it asks for, once
                time.sleep(min(int(m.group(1)) + 1, 65))
                continue
            return None
        except Exception:
            return None
    return None


def providers():
    """Ordered describers. An empty list means: no model configured, colour only."""
    chain = []
    if env_key("GEMINI_API_KEY", "GOOGLE_API_KEY", "INSPIRIME_GOOGLE_KEY"):
        chain.append(gemini_meta)
    if env_key("OPENAI_API_KEY"):
        chain.append(lambda d, m: chat_meta("https://api.openai.com/v1", env_key("OPENAI_API_KEY"),
                                            vision_model(OPENAI_DEFAULT_MODEL), d, m))
    if router_url():
        chain.append(lambda d, m: chat_meta(router_url(),
                                            env_key("INSPIRIME_VISION_KEY", "FLA_KEY", "OMNIROUTE_API_KEY"),
                                            vision_model("auto"), d, m))
    return chain


def describe(path):
    """type/style/mood/desc for one image. None means 'try again next scan'."""
    chain = providers()
    if not chain:
        return None
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    data = base64.b64encode(path.read_bytes()).decode()
    for call in chain:
        meta = call(data, mime)
        if meta and meta["desc"]:
            return meta
    return None


NO_MODEL = ("no vision model configured - images are indexed and measured, descriptions stay empty.\n"
            "  fix: GEMINI_API_KEY=... in ./.env  (or INSPIRIME_VISION_URL=http://host/v1 for an "
            "OpenAI-compatible endpoint), then run: inspirime scan")


def source_of(p, explicit):
    if explicit:
        return explicit
    try:
        rel = p.resolve().relative_to(INBOX.resolve())
        return rel.parts[0] if len(rel.parts) > 1 else "unknown"
    except ValueError:
        return "unknown"


def scan(paths, source=None, describe_too=True):
    """Index the given image files. Returns the log lines so a caller can show them."""
    con = db()
    files = [p for p in paths if p.suffix.lower() in EXT]
    # largest file first: an image saved as both .jpg and .webp indexes once, as the better copy
    files.sort(key=lambda p: p.stat().st_size, reverse=True)
    out, new = [], 0
    can_describe = bool(providers()) and describe_too
    if not can_describe:
        out.append(NO_MODEL)
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
        meta = (describe(dest) or {}) if can_describe else {}
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
    if can_describe:
        for key, file in con.execute("select key,file from shots where desc is null or desc=''").fetchall():
            meta = describe(ROOT / file)
            if meta:
                values = {k: json.dumps(v) if isinstance(v, list) else v for k, v in meta.items()}
                # column names come from META_FIELDS, never from input; only the values are bound
                con.execute(f"update shots set {','.join(k + '=?' for k in values)} where key=?",
                            (*values.values(), key))
                con.commit()
                out.append(f"~ described {file}")
    total = con.execute("select count(*) from shots").fetchone()[0]
    pending = con.execute("select count(*) from shots where desc is null or desc=''").fetchone()[0]
    out.append(f"{new} new, {total} total, {pending} pending (run scan again later)")
    con.close()
    return out


UI = {
    "en": {
        "ph_search": "Search description, style, type, source …",
        "sort_new": "Newest first", "sort_hue": "Sort by colour", "sort_type": "Sort by type",
        "scan_btn": "Scan new", "scan_title": "Look for new images in inbox/",
        "lbl_style": "style", "lbl_type": "type", "lbl_hue": "colour", "lbl_source": "source",
        "target_title": "Which agent harness you are sending to",
        "target_other": "Other MCP client",
        "btn_close": "Close", "btn_copy": "Copy", "btn_send": "Send",
        "count_fmt": "{a} of {b} images",
        "waiting": "(waiting for a description - run scan again)",
        "click_title": "Click to build a prompt",
        "empty": "No matches.",
        "empty_hint": 'Drop screenshots in <code>inbox/pinterest</code> or <code>inbox/dribbble</code> and press <b>Scan new</b>.',
        "hue": {"warm": "warm", "cool": "cool", "green": "green", "neutral": "neutral"},
        "scanning": "scanning …", "no_server": "no server - run: inspirime open",
        "sending": "sending …", "clip_ok": "copied", "clip_fail": "could not copy",
        "queued": "queued to {t} · {clip} · the agent fetches brief + image over MCP",
        "queue_reject": "{clip} · the queue refused it (unknown image)",
        "send_no_server": "{clip} · no server",
        "file_hint": "{clip} - the queue needs: inspirime open",
        "copied": "Copied", "copy_failed": "Could not",
        "prompt": "prompt", "inspiration": "inspiration",
    },
    "sv": {
        "ph_search": "Sök i beskrivning, stil, typ, källa …",
        "sort_new": "Nyast först", "sort_hue": "Sortera på färg", "sort_type": "Sortera på typ",
        "scan_btn": "Scanna nya", "scan_title": "Leta nya bilder i inbox/",
        "lbl_style": "stil", "lbl_type": "typ", "lbl_hue": "färg", "lbl_source": "källa",
        "target_title": "Vilken agent-harness du skickar till",
        "target_other": "Annan MCP-klient",
        "btn_close": "Stäng", "btn_copy": "Kopiera", "btn_send": "Skicka",
        "count_fmt": "{a} av {b} bilder",
        "waiting": "(väntar på beskrivning - kör scan igen)",
        "click_title": "Klicka för att skapa en prompt",
        "empty": "Inga träffar.",
        "empty_hint": 'Lägg skärmdumpar i <code>inbox/pinterest</code> eller <code>inbox/dribbble</code> och tryck <b>Scanna nya</b>.',
        "hue": {"warm": "varm", "cool": "kall", "green": "grön", "neutral": "neutral"},
        "scanning": "scannar …", "no_server": "ingen server - kör: inspirime open",
        "sending": "skickar …", "clip_ok": "kopierat", "clip_fail": "kunde inte kopiera",
        "queued": "köad till {t} · {clip} · agenten hämtar brief + bild via MCP",
        "queue_reject": "{clip} · kön tog inte emot (okänd bild)",
        "send_no_server": "{clip} · ingen server",
        "file_hint": "{clip} - kön kräver: inspirime open",
        "copied": "Kopierat", "copy_failed": "Kunde inte",
        "prompt": "prompt", "inspiration": "inspiration",
    },
}


def lang_table():
    return UI.get((os.environ.get("INSPIRIME_LANG") or "en").lower(), UI["en"])


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
.dlgbar{display:flex;gap:8px;justify-content:flex-end;align-items:center;margin-top:12px}
#pmsg{font-size:12px;color:var(--dim);margin-right:auto;align-self:center}
.chip.primary{background:var(--acc);border-color:var(--acc);color:#101204}
</style></head><body>
<header>
<h1>Inspirime<em id=count></em></h1>
<div class=bar>
  <input id=q placeholder="@@ph_search@@">
  <select id=sort>
    <option value=new>@@sort_new@@</option>
    <option value=hue>@@sort_hue@@</option>
    <option value=type>@@sort_type@@</option>
  </select>
  <button id=scanbtn class=chip title="@@scan_title@@">@@scan_btn@@</button>
  <span id=scanmsg></span>
</div>
<div class=row><span class=lbl>@@lbl_style@@</span><span class=row id=fstyle></span></div>
<div class=row><span class=lbl>@@lbl_type@@</span><span class=row id=ftype></span></div>
<div class=row><span class=lbl>@@lbl_hue@@</span><span class=row id=fhue></span></div>
<div class=row><span class=lbl>@@lbl_source@@</span><span class=row id=fsrc></span></div>
</header>
<main id=grid></main>
<dialog id=promptdlg>
  <h2 id=pdlgtitle></h2>
  <textarea id=ptext rows=24 spellcheck=false></textarea>
  <div class=dlgbar>
    <span id=pmsg></span>
    <select id=ptarget title="@@target_title@@">
      <option>Hermes</option>
      <option>Claude Code</option>
      <option>Codex</option>
      <option>Cursor / VS Code</option>
      <option>@@target_other@@</option>
    </select>
    <button id=pclose class=chip>@@btn_close@@</button>
    <button id=pcopy class=chip>@@btn_copy@@</button>
    <button id=psend class="chip primary">@@btn_send@@</button>
  </div>
</dialog>
<script>
const DATA = /*DATA*/[];
const T = /*I18N*/{};
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
  chips("fhue", ["warm", "cool", "green", "neutral"], "hue", v => T.hue[v]);
  chips("fsrc", [...new Set(DATA.map(d => d.source).filter(Boolean))].sort(), "source");
  document.getElementById("count").textContent =
    T.count_fmt.replace("{a}", rows.length).replace("{b}", DATA.length);

  document.getElementById("grid").innerHTML = rows.length ? rows.map(d => `
<figure data-key="${esc(d.key)}" title="${esc(T.click_title)}">
  <img loading=lazy src="${esc(d.file)}" alt="${esc(d.desc || d.key)}">
  <figcaption>
    <b>${esc(d.desc || T.waiting)}</b>
    ${esc(d.type || "")}${d.mood ? " · " + esc(d.mood) : ""}${d.w ? " · " + d.w + "×" + d.h : ""}
    <div class=tags>${d.colors.map(c => `<span class=dot style="background:${esc(c.hex)}" title="${esc(c.hex)} ${esc(c.pct)} %"></span>`).join("")}
      ${(d.style || []).map(t => `<span class=tag>${esc(t)}</span>`).join("")}</div>
  </figcaption>
</figure>`).join("") : `<div class=empty>${esc(T.empty)}<br>${T.empty_hint}</div>`;
}

const dlg = document.getElementById("promptdlg");
let openKey = null;
document.getElementById("grid").onclick = e => {
  const fig = e.target.closest("figure");
  const d = fig && DATA.find(x => x.key === fig.dataset.key);
  if (!d) return;
  openKey = d.key;
  document.getElementById("pmsg").textContent = "";
  document.getElementById("pdlgtitle").textContent =
    T.prompt + " · " + (d.type || T.inspiration) + ((d.style || []).length ? " · " + d.style.join(", ") : "");
  document.getElementById("ptext").value = d.prompt;
  dlg.showModal();
  // showModal focuses the textarea and scrolls it to the end; put the reader back at the top
  requestAnimationFrame(() => {
    const t = document.getElementById("ptext");
    t.setSelectionRange(0, 0);
    t.scrollTop = 0;
  });
};
document.getElementById("pclose").onclick = () => dlg.close();
async function toClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (e) {
    try {
      const t = document.getElementById("ptext");
      t.select();
      return document.execCommand("copy");
    } catch (e2) {
      return false;
    }
  }
}
document.getElementById("pcopy").onclick = async () => {
  const b = document.getElementById("pcopy");
  const ok = await toClipboard(document.getElementById("ptext").value);
  b.textContent = ok ? T.copied : T.copy_failed;
  setTimeout(() => { b.textContent = T.btn_copy; }, 1500);
};
document.getElementById("psend").onclick = async () => {
  const msg = document.getElementById("pmsg"), target = document.getElementById("ptarget").value;
  const brief = document.getElementById("ptext").value;
  msg.textContent = T.sending;
  const copied = await toClipboard(brief);
  const clip = copied ? T.clip_ok : T.clip_fail;
  if (location.protocol === "file:") {
    msg.textContent = T.file_hint.replace("{clip}", clip);
    return;
  }
  try {
    const r = await fetch("/handoff", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key: openKey, target })
    });
    const out = await r.json();
    msg.textContent = out.ok
      ? T.queued.replace("{t}", out.target).replace("{clip}", clip)
      : T.queue_reject.replace("{clip}", clip);
  } catch (e) {
    msg.textContent = T.send_no_server.replace("{clip}", clip);
  }
};

const scanbtn = document.getElementById("scanbtn"), scanmsg = document.getElementById("scanmsg");
const lastLine = s => (s || "").trim().split("\n").pop() || "";
scanbtn.onclick = async () => {
  if (location.protocol === "file:") { scanmsg.textContent = T.no_server; return; }
  scanbtn.disabled = true;
  scanmsg.textContent = T.scanning;
  try {
    const r = await fetch("/scan", { method: "POST" });
    if (!r.ok) throw new Error(r.status);
    for (;;) {
      const s = await (await fetch("/scan/status")).json();
      scanmsg.textContent = s.running ? T.scanning + " " + lastLine(s.log) : lastLine(s.log);
      if (!s.running) { setTimeout(() => location.reload(), 800); return; }
      await new Promise(res => setTimeout(res, 2000));
    }
  } catch (e) {
    scanmsg.textContent = T.no_server;
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


def _lang():
    return (os.environ.get("INSPIRIME_LANG") or "en").lower()


DENSITY_HINT = {
    "sv": {"luftig": "luftig = 28-36 px mellan sektioner, 18-24 px inne i kort, gott om luft runt text",
           "medel": "medel = 18-26 px mellan sektioner, 12-18 px inne i kort",
           "tat": "tät = 10-16 px mellan sektioner, 8-12 px inne i kort, små radavstånd"},
    "en": {"luftig": "airy = 28-36 px between sections, 18-24 px inside cards, generous space around text",
           "medel": "medium = 18-26 px between sections, 12-18 px inside cards",
           "tat": "dense = 10-16 px between sections, 8-12 px inside cards, tight line spacing"},
}

BRIEF = {
    "sv": {
        "h_goal": "# MÅL",
        "goal": "Bygg en {kind} som ser ut att höra till samma produkt som referensen: samma uppbyggnad, "
                "samma färgvärden, samma typografiska hierarki, samma täthet. Eget innehåll och eget namn — "
                "formen ska vara omöjlig att ta miste på.",
        "h_ref": "# REFERENS", "desc": "- Föreställer: {v}", "file": "- Fil: {v}",
        "size": "- Storlek: {w}×{h} px, {orient}, {ratio}, källa: {source}",
        "layout": "- Uppbyggnad: {v}", "sections": "- Sektioner i läsordning: {v}",
        "typography": "- Typografi: {v}", "dnr": "- Täthet: {density} · Hörnradie: {radius} · Bildspråk: {imagery}",
        "notes": "- Detaljer: {v}",
        "h_color": "# FÄRG — mätta på referensens pixlar, använd exakt dessa värden",
        "share": "- {pct} % {hex}{note}", "share_unknown": "- andel okänd {hex}{note}",
        "bg_note": "  ← största ytan (bakgrund)",
        "acc": "- Accent: {hex} (hue {h}°, mättnad {s} %, ljushet {l} %) — den enda accentfärgen",
        "acc_none": "- Accent: ingen. Referensen är i praktiken monokrom — hitta INTE på en accentfärg, "
                    "använd bara ytorna ovan plus svart/vitt för text.",
        "txt": "- Textfärg: {hex} mot bakgrunden {bg} (kontrast minst 4.5:1)",
        "txt_none": "- Textfärg: inte mätbar i referensen. Använd en neutral mot {bg} och räkna på 4.5:1.",
        "theme": "- Tema: {theme} (bakgrundens ljushet {luma})", "hue": "- Kulörfamilj: {v}",
        "h_req": "# KRAV",
        "req_colors": "1. Använd hex-värdena ovan exakt. Inga egna nyanser, inga fler accenter, inga gradienter "
                      "om inte 'gradient' står bland stilorden.",
        "req_layout": "2. Samma uppbyggnad: {v}",
        "req_sections": "3. Samma block i samma ordning: {v} — inga block tillkommer eller tas bort.",
        "req_radius": "4. Hörnradie {radius} på varje kort, knapp och inmatningsfält. Samma överallt, inga undantag.",
        "req_density": "5. Täthet: {v}.",
        "req_typography": "6. Typografi: {v} — max två teckensnitt, högst tre vikter, radhöjd 1.4-1.6 för brödtext.",
        "req_size": "7. Huvudvyn i {w}×{h} px ({ratio}); responsiv ner till 390 px utan att hierarkin eller "
                    "sektionsordningen ändras.",
        "req_style": "8. Stilorden som ska kännas igen: {v}.",
        "req_text": "9. All text på {language}, eget påhittat innehåll med rimliga siffror. Inga platshållare "
                    "som 'Lorem ipsum' eller 'Text här'.",
        "h_forbid": "# FÖRBJUDET",
        "forbid1": "- Kopiera inte text, logotyper, produktnamn eller bilder från referensen.",
        "forbid2": "- Inga externa resurser: inga webbfonter, inga stockbilder, inga ikonbibliotek från nätet.",
        "forbid3": "- Inga extra färger, skuggor eller animationer som inte syns i referensen.",
        "h_deliver": "# LEVERANS",
        "deliver1": "- En enda HTML-fil med CSS i <style> — inga byggsteg, inga dependencies.",
        "deliver2": "- Alla färger som CSS-variabler i :root med namnen --bg, --surface, --accent, --text.",
        "deliver3": "- Kommentera inget i koden; strukturen ska gå att läsa direkt.",
        "h_check": "# KONTROLL INNAN DU ÄR KLAR",
        "check_colors": "- [ ] Bakgrund {bg}{acc}{text} används exakt.",
        "check_radius": "- [ ] Radie {radius} på alla ytor.",
        "check_sections": "- [ ] Alla {n} sektioner finns i rätt ordning.",
        "check_nocopy": "- [ ] Ingen text eller bild hämtad från referensen.",
        "check_mobile": "- [ ] Sidan fungerar i 390 px bredd utan horisontell scroll.",
        "orient_land": "liggande", "orient_port": "stående", "orient_sq": "kvadrat",
        "unknown": "okänt", "dark": "mörkt", "light": "ljust", "language": "svenska",
        "density_default": "håll samma luft som referensen", "acc_bit": ", accent {v}", "text_bit": " och text {v}",
    },
    "en": {
        "h_goal": "# GOAL",
        "goal": "Build a {kind} that looks like it belongs to the same product as the reference: same "
                "structure, same colour values, same typographic hierarchy, same density. Your own content "
                "and your own name - the form must be unmistakable.",
        "h_ref": "# REFERENCE", "desc": "- Depicts: {v}", "file": "- File: {v}",
        "size": "- Size: {w}×{h} px, {orient}, {ratio}, source: {source}",
        "layout": "- Structure: {v}", "sections": "- Sections in reading order: {v}",
        "typography": "- Typography: {v}", "dnr": "- Density: {density} · Corner radius: {radius} · Imagery: {imagery}",
        "notes": "- Details: {v}",
        "h_color": "# COLOUR - measured on the reference pixels, use exactly these values",
        "share": "- {pct} % {hex}{note}", "share_unknown": "- share unknown {hex}{note}",
        "bg_note": "  <- largest area (background)",
        "acc": "- Accent: {hex} (hue {h}°, saturation {s} %, lightness {l} %) - the only accent colour",
        "acc_none": "- Accent: none. The reference is effectively monochrome - do NOT invent an accent, use "
                    "only the surfaces above plus black/white for text.",
        "txt": "- Text colour: {hex} on {bg} (contrast at least 4.5:1)",
        "txt_none": "- Text colour: not measurable in the reference. Use a neutral on {bg} and check for 4.5:1.",
        "theme": "- Theme: {theme} (background lightness {luma})", "hue": "- Hue family: {v}",
        "h_req": "# REQUIREMENTS",
        "req_colors": "1. Use the hex values above exactly. No shades of your own, no extra accents, no "
                      "gradients unless 'gradient' is among the style tags.",
        "req_layout": "2. Same structure: {v}",
        "req_sections": "3. Same blocks in the same order: {v} - nothing added, nothing removed.",
        "req_radius": "4. Corner radius {radius} on every card, button and input. Same everywhere, no exceptions.",
        "req_density": "5. Density: {v}.",
        "req_typography": "6. Typography: {v} - at most two typefaces, at most three weights, line-height "
                          "1.4-1.6 for body text.",
        "req_size": "7. Main view at {w}×{h} px ({ratio}); responsive down to 390 px without changing the "
                    "hierarchy or the section order.",
        "req_style": "8. The style tags to recognise: {v}.",
        "req_text": "9. All copy in {language}, invented but with plausible numbers. No placeholders like "
                    "'Lorem ipsum' or 'Text here'.",
        "h_forbid": "# FORBIDDEN",
        "forbid1": "- Do not copy text, logos, product names or images from the reference.",
        "forbid2": "- No external resources: no web fonts, no stock photos, no icon libraries from the network.",
        "forbid3": "- No extra colours, shadows or motion that are not visible in the reference.",
        "h_deliver": "# DELIVERY",
        "deliver1": "- One HTML file with CSS in <style> - no build step, no dependencies.",
        "deliver2": "- Every colour as a CSS variable in :root named --bg, --surface, --accent, --text.",
        "deliver3": "- No comments in the code; the structure must read on its own.",
        "h_check": "# CHECK BEFORE YOU FINISH",
        "check_colors": "- [ ] Background {bg}{acc}{text} used exactly.",
        "check_radius": "- [ ] Radius {radius} on every surface.",
        "check_sections": "- [ ] All {n} sections present in the right order.",
        "check_nocopy": "- [ ] No text or image taken from the reference.",
        "check_mobile": "- [ ] Works at 390 px wide without horizontal scroll.",
        "orient_land": "landscape", "orient_port": "portrait", "orient_sq": "square",
        "unknown": "unknown", "dark": "dark", "light": "light", "language": "English",
        "density_default": "match the reference's breathing room", "acc_bit": ", accent {v}", "text_bit": " and text {v}",
    },
}


def prompt_for(r):
    """A brief dense enough that a design agent has no room to interpret it wrong."""
    lang = _lang() if _lang() in BRIEF else "en"
    B = BRIEF[lang]
    colors = r["colors"] or []
    a = analyse(colors)
    bg, acc, text, dark = a["bg"], a["accent"], a["text"], a["dark"]
    radius = r.get("radius") or ""
    rad = "999px (pill)" if radius == "pill" else (f"{radius} px" if radius.isdigit() else B["unknown"])
    orient = (B["orient_land"] if r["w"] > r["h"] else B["orient_port"] if r["h"] > r["w"] else B["orient_sq"])
    sections = r["sections"] or []
    style = r["style"] or []

    p = [B["h_goal"], B["goal"].format(kind=r["type"] or ("sida" if lang == "sv" else "page")), "", B["h_ref"]]
    if r.get("desc"):
        p.append(B["desc"].format(v=r["desc"]))
    p.append(B["file"].format(v=ROOT / r["file"]))
    p.append(B["size"].format(w=r["w"], h=r["h"], orient=orient, ratio=ratio(r["w"], r["h"]),
                              source=r.get("source") or B["unknown"]))
    if r.get("layout"):
        p.append(B["layout"].format(v=r["layout"]))
    if sections:
        p.append(B["sections"].format(v=" → ".join(sections)))
    if r.get("typography"):
        p.append(B["typography"].format(v=r["typography"]))
    p.append(B["dnr"].format(density=r.get("density") or B["unknown"], radius=rad,
                             imagery=r.get("imagery") or B["unknown"]))
    if r.get("notes"):
        p.append(B["notes"].format(v=r["notes"]))

    p += ["", B["h_color"]]
    for i, c in enumerate(colors):
        tmpl = B["share"] if isinstance(c, dict) and c.get("pct") else B["share_unknown"]
        p.append(tmpl.format(pct=c["pct"] if isinstance(c, dict) else "", hex=hexes([c])[0],
                             note=B["bg_note"] if i == 0 else ""))
    if acc:
        ah, asat, al = hsl(acc)
        p.append(B["acc"].format(hex=acc, h=ah, s=asat, l=al))
    else:
        p.append(B["acc_none"])
    p.append(B["txt"].format(hex=text, bg=bg) if text else B["txt_none"].format(bg=bg))
    p.append(B["theme"].format(theme=B["dark"] if dark else B["light"], luma=round(luma(bg), 2)))
    p.append(B["hue"].format(v=r.get("huefam") or hue_family(acc or bg)))

    p += ["", B["h_req"], B["req_colors"]]
    if r.get("layout"):
        p.append(B["req_layout"].format(v=r["layout"]))
    if sections:
        p.append(B["req_sections"].format(v=", ".join(sections)))
    p.append(B["req_radius"].format(radius=rad))
    p.append(B["req_density"].format(v=DENSITY_HINT[lang].get((r.get("density") or "").lower(),
                                                             B["density_default"])))
    if r.get("typography"):
        p.append(B["req_typography"].format(v=r["typography"]))
    p.append(B["req_size"].format(w=r["w"], h=r["h"], ratio=ratio(r["w"], r["h"])))
    if style:
        p.append(B["req_style"].format(v=", ".join(style)))
    p.append(B["req_text"].format(language=B["language"]))

    p += ["", B["h_forbid"], B["forbid1"], B["forbid2"], B["forbid3"]]
    p += ["", B["h_deliver"], B["deliver1"], B["deliver2"], B["deliver3"]]
    p += ["", B["h_check"],
          B["check_colors"].format(bg=bg, acc=B["acc_bit"].format(v=acc) if acc else "",
                                   text=B["text_bit"].format(v=text) if text else ""),
          B["check_radius"].format(radius=rad),
          B["check_sections"].format(n=len(sections) or B["unknown"]),
          B["check_nocopy"], B["check_mobile"]]
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
    lang = (os.environ.get("INSPIRIME_LANG") or "en").lower()
    table = lang_table()
    html = TEMPLATE
    for key, val in table.items():
        if isinstance(val, str):            # static HTML slots
            html = html.replace(f"@@{key}@@", val)
    html = (html.replace('lang="sv"', f'lang="{lang}"')
                .replace("/*I18N*/{}", json.dumps(table, ensure_ascii=False))
                .replace("/*DATA*/[]", json.dumps(rows, ensure_ascii=False)))
    OUT.write_text(html)
    print(f"{len(rows)} shots -> {OUT}")


DEMO = [
    {"name": "demo-analytics-dark", "type": "dashboard", "style": ["minimal", "dark-mode"],
     "desc": "Ett mörkt analysdashboard med KPI-kort, linjediagram och sidonavigering.",
     "desc_en": "A dark analytics dashboard with KPI cards, a line chart and side navigation.",
     "bg": "#0f1216", "card": "#1b2027", "acc": "#c8f169"},
    {"name": "demo-editorial-light", "type": "article", "style": ["editorial", "swiss"],
     "desc": "En ljus redaktionell artikel med stor rubrik och smala textspalter.",
     "desc_en": "A light editorial article with a large headline and narrow text columns.",
     "bg": "#faf9f6", "card": "#ffffff", "acc": "#1a1a1a"},
    {"name": "demo-fintech-gradient", "type": "landing", "style": ["gradient", "corporate"],
     "desc": "En landningssida för fintech med gradienthjälte och trust-badges.",
     "desc_en": "A fintech landing page with a gradient hero and trust badges.",
     "bg": "#eef2ff", "card": "#ffffff", "acc": "#5b6cff"},
    {"name": "demo-mobile-cards", "type": "mobile", "style": ["playful", "minimal"],
     "desc": "En mobilskärm med kortbaserat flöde och rundade knappar.",
     "desc_en": "A mobile screen with a card-based feed and rounded buttons.",
     "bg": "#fff7f0", "card": "#ffffff", "acc": "#ff7a45"},
    {"name": "demo-poster-brutal", "type": "poster", "style": ["brutalist", "typographic"],
     "desc": "En brutalistisk poster med enorm typografi och hård kontrast.",
     "desc_en": "A brutalist poster with huge typography and harsh contrast.",
     "bg": "#f2f0e9", "card": "#111111", "acc": "#ff3b1f"},
    {"name": "demo-saas-dashboard", "type": "dashboard", "style": ["minimal", "corporate"],
     "desc": "Ett ljust SaaS-dashboard med tabell, filterrad och mjuka kort.",
     "desc_en": "A light SaaS dashboard with a table, a filter row and soft cards.",
     "bg": "#f7f8fa", "card": "#ffffff", "acc": "#2f6fed"},
]


def draw_demo(path, bg, card, acc, kind="dashboard"):
    """A blocky mock screen - enough structure that the measured palette and the gallery mean
    something before you own any real references."""
    im = Image.new("RGB", (900, 600), bg)
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, 899, 52], 0, fill=card)
    if kind == "poster":
        d.rectangle([60, 120, 840, 300], fill=acc)
        for i, w in enumerate((620, 480, 700)):
            d.rectangle([60, 340 + i * 60, 60 + w, 384 + i * 60], fill=card)
        im.save(path)
        return
    if kind == "mobile":
        d.rounded_rectangle([300, 80, 600, 580], 28, fill=card)
        d.rounded_rectangle([324, 110, 576, 200], 14, fill=acc)
        for i in range(3):
            d.rounded_rectangle([324, 220 + i * 100, 576, 300 + i * 100], 14, fill=bg)
        im.save(path)
        return
    if kind == "landing":
        d.rounded_rectangle([80, 100, 820, 330], 16, fill=acc)
        d.rounded_rectangle([300, 250, 600, 276], 6, fill=card)
        for i in range(3):
            x = 80 + i * 252
            d.rounded_rectangle([x, 380, x + 232, 520], 12, fill=card)
            d.rounded_rectangle([x + 18, 404, x + 140, 416], 4, fill=acc)
        im.save(path)
        return
    d.rounded_rectangle([22, 68, 198, 578], 12, fill=card)
    for row in range(5):
        d.rounded_rectangle([38, 92 + row * 34, 38 + (120 if row == 0 else 96), 104 + row * 34],
                            4, fill=acc if row == 0 else "#8b9199")
    if kind == "article":
        d.rounded_rectangle([250, 100, 760, 150], 6, fill=acc)
        for i in range(9):
            d.rounded_rectangle([250, 180 + i * 34, 250 + (460 - (i % 3) * 70), 192 + i * 34], 4, fill="#8b9199")
        im.save(path)
        return
    for i in range(3):
        x = 220 + i * 222
        d.rounded_rectangle([x, 68, x + 206, 300], 12, fill=card)
        d.rounded_rectangle([x + 16, 92, x + 96, 102], 4, fill=acc)
        d.rounded_rectangle([x + 16, 118, x + 150, 150], 6, fill=acc if i == 0 else "#8b9199")
        for j in range(3):
            d.rounded_rectangle([x + 16, 168 + j * 18, x + 180 - j * 26, 176 + j * 18], 4, fill="#8b9199")
    d.rounded_rectangle([220, 316, 878, 578], 12, fill=card)
    pts = [(240 + i * 88, 540 - int(abs(120 * math.sin(i / 1.6)))) for i in range(8)]
    d.line(pts, fill=acc, width=5)
    im.save(path)


def demo():
    """Small sample library so the first run has something to look at - works with no API key."""
    src = INBOX / "demo"
    src.mkdir(parents=True, exist_ok=True)
    for d in DEMO:
        path = src / f"{d['name']}.png"
        if not path.exists():
            draw_demo(path, d["bg"], d["card"], d["acc"], d["type"])
    lines = scan([src / f"{d['name']}.png" for d in DEMO], source="demo", describe_too=False)
    for line in lines:                    # only real problems; the pending markers are expected
        if line.startswith("skip "):
            print(line)
    con = db()
    en = _lang() != "sv"
    for d in DEMO:
        path = src / f"{d['name']}.png"
        w, h = Image.open(path).size
        con.execute("update shots set type=?, style=?, mood='demo', desc=?, layout=?, sections=?,"
                    " typography=?, density='medel', radius='12', imagery=?, notes=? where key=?",
                    (d["type"], json.dumps(d["style"]),
                     d["desc_en"] if en else d["desc"],
                     "Sidebar + top bar + card grid (drawn sample)" if en
                     else "Vänsterspalt + topprad + kortgrid (ritad demobild)",
                     json.dumps(["top bar", "side navigation", "card row", "chart"] if en
                                else ["topprad", "sidonavigering", "kortrad", "diagram"]),
                     "sans, two weights" if en else "sans, två vikter",
                     "none" if en else "inga",
                     "generated by inspirime demo",
                     f"{d['name']}-{w}x{h}"))
    con.commit()
    con.close()
    build()
    print(f"\ndemo library ready in {ROOT} - now run: inspirime open")


def open_in_browser(url):
    cmd = (["open"] if sys.platform == "darwin" else
           ["cmd", "/c", "start", ""] if os.name == "nt" else ["xdg-open"])
    subprocess.run([*cmd, url], check=False)


def brief_cmd(key, as_json=False):
    con = db()
    r = record(con, key)
    con.close()
    if not r:
        sys.exit(f"no reference with key {key!r} - list keys with: inspirime export")
    print(json.dumps(r, ensure_ascii=False, indent=2) if as_json else r["prompt"])


def export_cmd(out_path=None):
    con = db()
    cur = con.execute("select * from shots order by added desc")
    cols = [d[0] for d in cur.description]
    rows = []
    for raw in cur:
        r = dict(zip(cols, raw))
        for field in ("colors", "style", "sections"):
            r[field] = json.loads(r[field] or "[]")
        r["prompt"] = prompt_for(r)
        rows.append(r)
    con.close()
    text = json.dumps(rows, ensure_ascii=False, indent=2)
    if out_path:
        Path(out_path).write_text(text)
        print(f"{len(rows)} references -> {out_path}")
    else:
        print(text)


def tag_cmd(key, changes):
    """+tag adds, -tag removes, a bare word replaces the whole tag set."""
    con = db()
    r = record(con, key)
    if not r:
        con.close()
        sys.exit(f"no reference with key {key!r}")
    tags = set(r["style"])
    for c in changes:
        if c.startswith("+"):
            tags.add(c[1:].strip().lower())
        elif c.startswith("-"):
            tags.discard(c[1:].strip().lower())
        elif c.strip():
            tags = {c.strip().lower()}
    con.execute("update shots set style=? where key=?", (json.dumps(sorted(tags)), key))
    con.commit()
    con.close()
    print(f"{key}: {', '.join(sorted(tags)) or '(no tags)'}")


def delete_cmd(key):
    con = db()
    r = record(con, key)
    if not r:
        con.close()
        sys.exit(f"no reference with key {key!r}")
    con.execute("delete from shots where key=?", (key,))
    con.execute("delete from handoffs where key=?", (key,))
    con.commit()
    con.close()
    (ROOT / r["file"]).unlink(missing_ok=True)
    print(f"deleted {key}")


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
    for needle in ("# GOAL", "# COLOUR", "# REQUIREMENTS", "# FORBIDDEN", "# DELIVERY", "40 %", "#2e8081",
                   "1440×900", "8:5", "topprad → KPI-kort", "12 px", "18-26 px", str(ROOT / "library/x.png")):
        assert needle in brief, (needle, brief)
    os.environ["INSPIRIME_LANG"] = "sv"
    assert prompt_for({"type": "other", "desc": "", "style": [], "mood": "", "colors": [],
                       "w": 10, "h": 10, "file": "library/x.png", "source": "", "layout": "",
                       "sections": [], "typography": "", "density": "", "radius": "",
                       "imagery": "", "notes": ""}).startswith("# MÅL")
    os.environ.pop("INSPIRIME_LANG")
    global DB
    DB, real = ROOT / "selfcheck.db", DB
    try:
        con = db()
        con.execute("insert or replace into shots(key,file,w,h,colors,hue,huefam,source,type,style,mood,desc,added)"
                    " values('a-400x300',?,400,300,'[]',0,'warm','pinterest','landing','[]','','x','t')", (str(tmp),))
        con.execute("insert or replace into shots(key,file,w,h,colors,hue,huefam,source,type,style,mood,desc,added)"
                    " values('a-400x300',?,400,300,'[]',0,'warm','pinterest','landing','[]','','y','t')", (str(tmp),))
        con.commit()
        assert con.execute("select count(*) from shots").fetchone()[0] == 1
        assert handoff("a-400x300", "Hermes")["prompt"], "handoff must freeze a brief"
        assert "a-400x300" in mcp_tool("handoffs", {})[0]["text"]
        got = mcp_tool("get", {"key": "a-400x300"})
        assert [c["type"] for c in got] == ["text", "image"], [c["type"] for c in got]
        assert mcp_tool("ack", {"key": "a-400x300"})[0]["text"].startswith("1")
        assert "No pending handoffs" in mcp_tool("handoffs", {})[0]["text"]
        con.close()
    finally:
        DB = real
        (ROOT / "selfcheck.db").unlink(missing_ok=True)
    tmp.unlink(missing_ok=True)
    print("selfcheck ok")


def record(con, key):
    """One shot as the dict prompt_for expects."""
    cur = con.execute("select * from shots where key=?", (key,))
    row = cur.fetchone()
    if not row:
        return None
    r = dict(zip([d[0] for d in cur.description], row))
    r["colors"] = json.loads(r["colors"] or "[]")
    r["style"] = json.loads(r["style"] or "[]")
    r["sections"] = json.loads(r["sections"] or "[]")
    r["prompt"] = prompt_for(r)
    return r


def handoff(key, target="Hermes"):
    """Freeze the brief as it is now, so a later change to prompt_for cannot alter what was sent."""
    con = db()
    r = record(con, key)
    if not r:
        con.close()
        return None
    con.execute("insert into handoffs(key,target,prompt,created) values(?,?,?,?)",
                (key, target, r["prompt"], datetime.now(timezone.utc).isoformat(timespec="seconds")))
    con.commit()
    con.close()
    return r


def one_line(r):
    return (f"{r['key']} · {r.get('type') or '?'} · {', '.join(r.get('style') or []) or '-'} · "
            f"{r.get('w')}×{r.get('h')} · {r.get('source') or '?'} · "
            f"{', '.join(c['hex'] for c in (r.get('colors') or [])[:4])}\n"
            f"    {r.get('desc') or '(no description yet)'}")


MCP_TOOLS = [
    {"name": "handoffs",
     "description": "Inspiration references the user pressed Send on in the Inspirime gallery, "
                    "newest first. Call this first. Each entry has a key: call get(key) for the full "
                    "brief plus the reference image, then ack(key) when you have taken it.",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
    {"name": "search",
     "description": "Search the inspiration library by free text and filters (type, style tag, hue "
                    "family, source). Returns one line per match with its key.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "free text over description, layout and style"},
         "type": {"type": "string"}, "style": {"type": "string"},
         "hue": {"type": "string", "enum": ["warm", "cool", "green", "neutral"]},
         "source": {"type": "string"}, "limit": {"type": "integer"}}}},
    {"name": "get",
     "description": "Full record for one inspiration: measured palette, model-read layout/typography, "
                    "the ready-to-use design brief, and the image itself as an attachment.",
     "inputSchema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}},
    {"name": "ack",
     "description": "Mark a handoff as taken so it stops showing up in handoffs().",
     "inputSchema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}},
]


def mcp_tool(name, args):
    """One MCP tools/call. Returns the content list."""
    con = db()
    try:
        if name == "handoffs":
            rows = con.execute("select id,key,target,created from handoffs where acked is null"
                               " order by created desc limit ?", (int(args.get("limit") or 5),)).fetchall()
            if not rows:
                return [{"type": "text", "text": "No pending handoffs. Use search to look through the library."}]
            text = [f"{len(rows)} pending handoff(s), newest first:"]
            for i, (_, key, target, created) in enumerate(rows, 1):
                r = record(con, key)
                text.append(f"{i}) [{target or 'okänd'} · {created}]\n    " + (one_line(r) if r else key))
            text.append("\nTake one with get(key) - the reference image comes with it - then ack(key).")
            return [{"type": "text", "text": "\n".join(text)}]

        if name == "search":
            where, params = [], []
            for field in ("type", "huefam", "source"):
                val = args.get(field if field != "huefam" else "hue")
                if val:
                    where.append(f"{field}=?")
                    params.append(str(val).lower())
            if args.get("style"):
                where.append("style like ?")
                params.append(f'%"{str(args["style"]).lower()}"%')
            if (args.get("query") or "").strip():
                where.append("lower(coalesce(desc,'')||' '||coalesce(layout,'')||' '||coalesce(style,'')"
                             "||' '||coalesce(type,'')||' '||coalesce(notes,'')) like ?")
                params.append(f"%{args['query'].strip().lower()}%")
            sql = "select * from shots" + (" where " + " and ".join(where) if where else "")
            sql += " order by added desc limit ?"
            params.append(int(args.get("limit") or 5))
            cur = con.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = []
            for raw in cur:
                r = dict(zip(cols, raw))
                r["colors"] = json.loads(r["colors"] or "[]")
                r["style"] = json.loads(r["style"] or "[]")
                rows.append(r)
            if not rows:
                return [{"type": "text", "text": "No matches."}]
            return [{"type": "text", "text": "\n".join(one_line(r) for r in rows)}]

        if name == "get":
            r = record(con, args.get("key", ""))
            if not r:
                return [{"type": "text", "text": f"No reference with key {args.get('key')!r}."}]
            palette_line = ", ".join(f"{c['hex']} {c['pct']}%" for c in r["colors"])[:400]
            head = (f"{r['key']}\n{r.get('desc') or ''}\n"
                    f"typ: {r.get('type')} · stil: {', '.join(r.get('style') or [])} · känsla: {r.get('mood') or '-'}\n"
                    f"referensbild: {ROOT / r['file']}\n"
                    f"färger: {palette_line}\n"
                    f"kulör: {r.get('huefam')} · täthet: {r.get('density') or '-'} · radie: {r.get('radius') or '-'}\n"
                    "--- färdig brief ---\n")
            content = [{"type": "text", "text": head + r["prompt"]}]
            try:
                data = base64.b64encode((ROOT / r["file"]).read_bytes()).decode()
                mime = mimetypes.guess_type(r["file"])[0] or "image/jpeg"
                content.append({"type": "image", "data": data, "mimeType": mime})
            except Exception as e:
                content.append({"type": "text", "text": f"(kunde inte bifoga bilden: {e})"})
            return content

        if name == "ack":
            cur = con.execute("update handoffs set acked=? where key=? and acked is null",
                              (datetime.now(timezone.utc).isoformat(timespec="seconds"), args.get("key", "")))
            con.commit()
            return [{"type": "text", "text": f"{cur.rowcount} handoff(s) acknowledged."}]

        return [{"type": "text", "text": f"Unknown tool: {name}"}]
    finally:
        con.close()


def mcp():
    """Minimal stdio MCP server (newline-delimited JSON-RPC) - no dependencies, no port."""
    def emit(obj):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            emit({"jsonrpc": "2.0", "id": None,
                  "error": {"code": -32700, "message": "parse error"}})
            continue
        if "id" not in msg:
            continue  # notification: answering it breaks some clients
        method, rid = msg.get("method"), msg["id"]
        try:
            if method == "initialize":
                res = {"protocolVersion": msg.get("params", {}).get("protocolVersion", "2024-11-05"),
                       "capabilities": {"tools": {}},
                       "serverInfo": {"name": "inspirime", "version": "1.0"},
                       "instructions": "Inspiration library for design work. Call handoffs first: those are "
                                       "references the user just sent you. get(key) returns the brief and "
                                       "attaches the image; ack(key) clears it."}
            elif method == "tools/list":
                res = {"tools": MCP_TOOLS}
            elif method == "tools/call":
                p = msg.get("params", {})
                res = {"content": mcp_tool(p.get("name"), p.get("arguments") or {})}
            elif method == "ping":
                res = {}
            else:
                emit({"jsonrpc": "2.0", "id": rid,
                      "error": {"code": -32601, "message": f"unknown method {method}"}})
                continue
            emit({"jsonrpc": "2.0", "id": rid, "result": res})
        except Exception as e:  # a bad call must not kill the session
            emit({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}})


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
            path = self.path.split("?")[0]
            if path == "/scan":
                return self._json({"started": run_scan(), **SCAN})
            if path == "/handoff":
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    req = json.loads(body or b"{}")
                except json.JSONDecodeError:
                    return self._json({"ok": False, "error": "bad json"})
                r = handoff(req.get("key", ""), req.get("target") or "Hermes")
                return self._json({"ok": bool(r), "target": req.get("target") or "Hermes",
                                   "prompt": (r or {}).get("prompt", "")})
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
        print(f"server already running: {url}")
        if open_browser:
            open_in_browser(url)
        return
    print(f"Inspirime: {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.4, lambda: open_in_browser(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()


def main():
    ap = argparse.ArgumentParser(prog="inspirime", description="Inspiration library for design agents.")
    ap.add_argument("--version", action="version", version=f"inspirime {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("demo", help="create and index a small sample library (no API key needed)")
    s = sub.add_parser("scan", help="index new images")
    s.add_argument("paths", nargs="*", type=Path)
    s.add_argument("--source", help="source label (default: the inbox subfolder name)")
    s.add_argument("--no-describe", action="store_true",
                   help="measure colour only - no vision model call")
    b = sub.add_parser("brief", help="print the design brief for one reference")
    b.add_argument("key")
    b.add_argument("--json", action="store_true", help="the whole record as JSON")
    e = sub.add_parser("export", help="dump the whole index as JSON")
    e.add_argument("--out")
    t = sub.add_parser("tag", help="edit style tags: +add -remove, or one word to replace them")
    t.add_argument("key")
    t.add_argument("changes", nargs="+")
    d = sub.add_parser("delete", help="remove one reference (row and library copy)")
    d.add_argument("key")
    sub.add_parser("build", help="write gallery.html")
    o = sub.add_parser("open", help="serve the gallery and open it (scan button works here)")
    o.add_argument("--port", type=int, default=8789)
    o.add_argument("--no-browser", action="store_true")
    sub.add_parser("mcp", help="stdio MCP server for agent harnesses (handoffs/search/get/ack)")
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
        for line in scan(files, a.source, describe_too=not a.no_describe):
            print(line)
    elif a.cmd == "demo":
        demo()
    elif a.cmd == "brief":
        brief_cmd(a.key, a.json)
    elif a.cmd == "export":
        export_cmd(a.out)
    elif a.cmd == "tag":
        tag_cmd(a.key, a.changes)
    elif a.cmd == "delete":
        delete_cmd(a.key)
    elif a.cmd == "build":
        build()
    elif a.cmd == "open":
        serve(a.port, not a.no_browser)
    elif a.cmd == "mcp":
        mcp()
    else:
        selfcheck()


if __name__ == "__main__":
    main()
