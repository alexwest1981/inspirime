# Inspirime

Bibliotek för skärmdumpar du sparat från Pinterest och Dribbble. Varje bild får
en mätt färgpalett, en stilklassning och en beskrivning — sedan bläddrar du i ett
galleri med filter.

## Snabbstart

```bash
inspirime open        # startar galleriet på http://127.0.0.1:8789 och öppnar webbläsaren
```

Lägg nya dumpar i `inbox/pinterest/` eller `inbox/dribbble/` och tryck **Scanna nya**
i sidan — den indexerar, beskriver och laddar om galleriet själv. Mappnamnet blir
källan i filtret.

Vill du indexera bilder som redan ligger någon annanstans (de kopieras, originalen
rörs inte):

```bash
inspirime scan ~/Pictures/Inspo --source inspo
inspirime build
```

## Knappar i galleriet

| Gör | Hur |
|---|---|
| indexera nya bilder | **Scanna nya** (kräver att sidan körs via `inspirime open`, inte `file://`) |
| filtrera | klicka på stil/typ/färg/källa, eller sök i fritextfältet |
| sortera | välj Nyast / färg / typ |
| få en promp till en designagent | klicka på en bild → panel med färdig brief → **Kopiera** |

## Vad som händer med en bild

| Fält | Källa |
|---|---|
| färger, kulörfamilj (varm/kall/grön/neutral) | mätt med Pillow på bildens pixlar — inte gissat |
| typ (landing, dashboard, mobile, poster …) | bildmodell |
| stil (minimal, brutalist, editorial …) | bildmodell, max 3 taggar |
| mood + beskrivning på svenska | bildmodell |

Bilden kopieras till `library/` och originalet rörs inte. Indexet ligger i
`inspirime.db` (SQLite). En bild som sparats både som `.jpg` och `.webp` indexeras
en gång — den största filen vinner.

## Modell

Beskrivningarna kommer från Gemini direkt — `GOOGLE_API_KEY` läses från
`~/.hermes/.env`, modell `gemini-3.5-flash-lite`. Det kostar några öre för hela
biblioteket och är stabilt. Saknas nyckeln (eller svarar Gemini inte) faller den
tillbaka på den lokala gratiroutern `http://127.0.0.1:3001/v1` med `model=auto`,
som är gratis men stryper ibland. Bilder som inte blev beskrivna hamnar som
`pending` och försöks igen vid nästa `scan`.

```bash
INSPIRIME_GOOGLE_MODEL=gemini-3.8-flash inspirime scan      # annan modell
INSPIRIME_GOOGLE_KEY=... inspirime scan                     # annan nyckel
INSPIRIME_VISION_URL=http://127.0.0.1:11434/v1 \
INSPIRIME_VISION_MODEL=qwen3-vl inspirime scan              # egen router
```

## Autopilot

```bash
# var 15:e minut: indexera nya dumpar, bygg om galleriet
*/15 * * * * cd ~/Projects/inspirime && ./inspirime.py scan >/dev/null 2>&1 && ./inspirime.py build >/dev/null
```

## Filer

```
inspirime.py     hela verktyget (open / scan / build / selfcheck)
inbox/<källa>/   hit sparar du dumparna; mappnamnet blir källan
library/         kopior av indexerade bilder
inspirime.db     index
gallery.html     galleriet (genereras; öppnas via http://127.0.0.1:8789/gallery.html)
```

`./inspirime.py selfcheck` kör kontrollen av palett, JSON-tolkning och
dubblettregeln utan nätverk.
