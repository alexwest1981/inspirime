"""Tests for inspirime. Run with: python3 tests/test_inspirime.py   (pytest also collects it)."""
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="inspirime-test-")
os.environ["INSPIRIME_HOME"] = TMP          # both must be set before import: the library path is fixed at import
os.environ["INSPIRIME_LANG"] = "en"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import inspirime as I  # noqa: E402

from PIL import Image  # noqa: E402


def img(name, color=(198, 26, 40), size=(400, 300)):
    p = Path(TMP) / "in" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(p)
    return p


def test_home_is_redirected():
    assert str(I.ROOT) == str(Path(TMP).resolve())


def test_palette_has_shares():
    pal = I.palette(Image.open(img("flat.png")))
    assert 1 <= len(pal) <= 12 and pal[0]["hex"].startswith("#"), pal
    assert abs(sum(c["pct"] for c in pal) - 100) <= 2, pal


def test_accent_needs_saturation():
    light = [{"hex": h, "pct": 10} for h in ("#fbfbfb", "#f3f3f4", "#a8adb6", "#89b2e4", "#e7e2ce")]
    assert I.analyse(light)["accent"] == "#89b2e4", I.analyse(light)
    mono = [{"hex": h, "pct": 20} for h in ("#111111", "#1a1a1a", "#333333", "#545455")]
    assert I.analyse(mono)["accent"] is None


def test_text_colour_needs_contrast():
    assert I.analyse([{"hex": "#111111", "pct": 60}, {"hex": "#f0f1f2", "pct": 20}])["text"] == "#f0f1f2"
    assert I.analyse([{"hex": "#fbfbfb", "pct": 60}, {"hex": "#a8adb6", "pct": 20}])["text"] is None
    assert I.contrast("#111111", "#fbfbfb") > 18
    assert I.contrast("#fbfbfb", "#fbfbfb") == 1.0


def test_hue_family():
    assert I.hue_family("#c61a28") == "warm"
    assert I.hue_family("#1b1c20") == "neutral"
    assert 350 <= I.hue_of("#c61a28") or I.hue_of("#c61a28") <= 5


def test_ratio_and_orientation():
    assert I.ratio(1440, 900) == "8:5"
    assert I.ratio(1600, 1200) == "4:3"


def test_parse_meta_tolerates_fences_and_strings():
    meta = I.parse_meta('```json\n{"type":"Landing","style":"Minimal, editorial","mood":"calm",'
                        '"desc":"Ett lugnt gränssnitt.","sections":["topprad","KPI-kort"],'
                        '"density":"TAT","radius":"12"}\n```')
    assert meta["type"] == "landing"
    assert meta["style"] == ["minimal", "editorial"]
    assert meta["sections"] == ["topprad", "kpi-kort"]
    assert meta["density"] == "tat"
    assert I.parse_meta("no json")["desc"] == ""


def test_brief_is_dense():
    brief = I.prompt_for({
        "type": "landing", "desc": "Ett lugnt gränssnitt.", "style": ["minimal"], "mood": "calm",
        "colors": [{"hex": "#111111", "pct": 40}, {"hex": "#eeeeee", "pct": 30}, {"hex": "#2e8081", "pct": 10}],
        "w": 1440, "h": 900, "file": "library/x.png", "source": "pinterest",
        "layout": "vänsterspalt 240 px", "sections": ["topprad", "KPI-kort"],
        "typography": "sans, två vikter", "density": "medel", "radius": "12", "imagery": "ikoner",
        "notes": "mjuka skuggor", "huefam": "cool"})
    for needle in ("# GOAL", "# COLOUR", "# REQUIREMENTS", "# FORBIDDEN", "# DELIVERY", "CHECK BEFORE",
                   "40 %", "#2e8081", "1440×900", "8:5", "topprad → KPI-kort", "12 px", "18-26 px"):
        assert needle in brief, needle
    bare = I.prompt_for({"type": "other", "desc": "", "style": [], "mood": "", "colors": [],
                         "w": 10, "h": 10, "file": "x.png", "source": "", "layout": "", "sections": [],
                         "typography": "", "density": "", "radius": "", "imagery": "", "notes": ""})
    assert "Accent: none" in bare and "# GOAL" in bare
    os.environ["INSPIRIME_LANG"] = "sv"                     # same record, Swedish brief
    sv = I.prompt_for({"type": "other", "desc": "", "style": [], "mood": "", "colors": [],
                       "w": 10, "h": 10, "file": "x.png", "source": "", "layout": "", "sections": [],
                       "typography": "", "density": "", "radius": "", "imagery": "", "notes": ""})
    os.environ["INSPIRIME_LANG"] = "en"
    assert "# MÅL" in sv and "# FÖRBJUDET" in sv, sv[:80]


def test_scan_dedupes_same_stem_and_size():
    a, b = img("dup.jpg"), img("dup.webp")
    lines = I.scan([a, b], source="test", describe_too=False)
    assert sum(line.startswith("+ ") for line in lines) == 1, lines
    con = I.db()
    assert con.execute("select count(*) from shots where key like 'dup-%'").fetchone()[0] == 1
    con.close()


def test_handoff_and_mcp_roundtrip():
    p = img("round.png", (20, 20, 20), (320, 240))
    I.scan([p], source="test", describe_too=False)
    key = f"round-320x240"
    assert I.handoff(key, "Hermes")["prompt"]
    assert key in I.mcp_tool("handoffs", {})[0]["text"]
    got = I.mcp_tool("get", {"key": key})
    assert [c["type"] for c in got] == ["text", "image"], [c["type"] for c in got]
    assert I.mcp_tool("get", {"key": "nope"})[0]["text"].startswith("No reference")
    assert I.mcp_tool("ack", {"key": key})[0]["text"].startswith("1")
    assert "No pending handoffs" in I.mcp_tool("handoffs", {})[0]["text"]


def test_tag_brief_export_delete():
    p = img("tags.png", (240, 240, 240), (300, 300))
    I.scan([p], source="test", describe_too=False)
    key = "tags-300x300"
    with redirect_stdout(io.StringIO()) as out:
        I.tag_cmd(key, ["+minimal", "+bright"])
        I.tag_cmd(key, ["-minimal"])
        I.brief_cmd(key)
        I.export_cmd(str(Path(TMP) / "dump.json"))
    assert "bright" in out.getvalue()
    con = I.db()
    style = json.loads(con.execute("select style from shots where key=?", (key,)).fetchone()[0])
    con.close()
    assert style == ["bright"], style
    rows = json.loads((Path(TMP) / "dump.json").read_text())
    assert any(r["key"] == key and r["prompt"].startswith("# GOAL") for r in rows)
    with redirect_stdout(io.StringIO()):
        I.delete_cmd(key)
    con = I.db()
    assert con.execute("select count(*) from shots where key=?", (key,)).fetchone()[0] == 0
    con.close()
    assert not (I.ROOT / "library" / "tags.png").exists()


def test_mcp_protocol_and_selfcheck():
    assert all(callable(call) for call in I.providers())
    assert I.NO_MODEL.startswith("no vision model configured")
    assert I.__version__.count(".") == 2


def main():
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{'failed' if failures else 'all good'}: {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
