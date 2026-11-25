## Install

```bash
pipx install git+https://github.com/alexwest1981/inspirime
inspirime demo     # six drawn samples, no API key
inspirime open     # gallery at http://127.0.0.1:8789
```

Or the wheel below: `pip install inspirime-0.2.0-py3-none-any.whl`.

## What you get

- **Measured, not guessed**: 12-colour palette with each colour's share, background, accent (the most
  saturated tone, not the most common one), text colour behind a WCAG 4.5:1 gate.
- **A dense brief per reference**: goal, measured colours with hex, numbered requirements,
  prohibitions, delivery format, self-check list. English by default, `INSPIRIME_LANG=sv` for Swedish.
- **MCP handoff**: `inspirime mcp` serves `handoffs` / `search` / `get` / `ack`; `get` returns the
  brief and the reference image, so a vision-capable agent sees the design.
- **No API key, no server, no build step**: one file, one dependency (Pillow), SQLite, one generated
  `gallery.html`.

## New in 0.2.0

- `pipx install git+…` works: `pyproject.toml`, console script, `--version`, MIT license.
- `inspirime demo` — a sample library drawn with Pillow, so the first run shows a real gallery.
- New subcommands: `brief [--json]`, `export`, `tag`, `delete`, `--no-describe`.
- Missing key now prints one actionable line instead of 36 s of silent retries.
- `INSPIRIME_HOME` keeps data out of the package; installed copies default to `~/.local/share/inspirime`.
- 13 assert-based tests (no network) and CI on Linux/macOS, Python 3.9–3.13.

## Limits, stated plainly

Colour, size, ratio and contrast are measured from the pixels. Layout, sections and typography are a
vision model's reading of the picture — a good draft, correctable with `inspirime tag`. A monochrome
reference gets "no accent" in the brief instead of an invented hex.
