# Inspirime — svensk dokumentation

Dina sparade skärmdumpar från Pinterest/Dribbble blir ett bibliotek som en designagent kan läsa.
Färger **mäts** på pixlarna, layout läses av en bildmodell, och varje referens får en tät brief som
agenten kan jobba mot — plus bilden bifogad via MCP.

```bash
pipx install git+https://github.com/<du>/inspirime
inspirime demo        # exempelbibliotek, ingen API-nyckel behövs
inspirime open        # galleriet på http://127.0.0.1:8789
```

## Kommandon

| Kommando | Gör |
|---|---|
| `inspirime demo` | ritar och indexerar sex exempelbilder |
| `inspirime scan [mapp…] [--source namn] [--no-describe]` | indexerar nya bilder (kopierar till `library/`) |
| `inspirime open [--port 8789]` | serverar galleriet och öppnar webbläsaren |
| `inspirime brief <nyckel> [--json]` | skriver briefen i terminalen |
| `inspirime export [--out fil]` | hela indexet som JSON |
| `inspirime tag <nyckel> +minimal -corporate` | rättar modellens taggar för hand |
| `inspirime delete <nyckel>` | tar bort en referens (rad + bibliotekskopia) |
| `inspirime build` | bygger bara om `gallery.html` |
| `inspirime mcp` | stdio-MCP-server: `handoffs`, `search`, `get`, `ack` |
| `inspirime selfcheck` | kontrollerar palett, accent, kontrast, brief och MCP utan nätverk |

Svensk UI: `INSPIRIME_LANG=sv` (standard är engelska för nya användare).
Egen dataplats: `INSPIRIME_HOME=/någon/mapp`.

## Hur en bild blir en referens

| Fält | Källa |
|---|---|
| 12-färgspalett med andel i procent, bakgrund, accent, textfärg med kontrastgrind | mätt med Pillow |
| typ, stil (max 3 taggar), mood, beskrivning | bildmodell |
| uppbyggnad, sektioner i läsordning, typografi, täthet, hörnradie, bildspråk, detaljer | bildmodell |
| format, bildförhållande, orientering, källa | mätt respektive från mappnamnet |

Accentfärgen är den **mest mättade** tonen, inte den vanligaste — annars blir varje vit UI-skärm
"neutral". Saknas en riktig accent (monokrom referens) säger briefen det uttryckligen i stället för
att hitta på en färg. Textfärg släpps bara igenom om kontrasten mot bakgrunden är minst 4.5:1.

Bilden kopieras till `library/`; originalet rörs inte. En bild sparad både som `.jpg` och `.webp`
indexeras en gång (största filen vinner).

## Modell

| Uppsättning | Hur |
|---|---|
| Google Gemini | `GEMINI_API_KEY=...` i `./.env` (standard) |
| OpenAI | `OPENAI_API_KEY=...` |
| OpenAI-kompatibel endpoint (LM Studio, Ollama, vLLM, router) | `INSPIRIME_VISION_URL=http://host:port/v1` + `INSPIRIME_VISION_MODEL` |

Utan nyckel indexeras och mäts allt ändå — beskrivningarna står tomma tills du lägger till en nyckel.
Ob skrivna rader försöks igen vid nästa `scan`.

## Skicka till en agent

**Skicka** i galleriet gör två saker: kopierar briefen till urklipp och köar den i databasen med
briefen fryst som den såg ut i sekunden du skickade. Agenten hämtar den via MCP:

```bash
hermes mcp add inspirime --command python3 --args /sökväg/inspirime.py mcp
claude mcp add inspirime python3 /sökväg/inspirime.py mcp
```

Inget går förlorat om ingen agent är igång: kön ligger kvar, och `get` fungerar i efterhand på vilken
nyckel som helst ur biblioteket.

## Autopilot

```bash
# var 15:e minut: indexera nya dumpar, bygg om galleriet
*/15 * * * * cd ~/Projects/inspirime && ./inspirime.py scan >/dev/null 2>&1 && ./inspirime.py build >/dev/null
```

## Begränsningar

- Färg, storlek, bildförhållande och kontrast är **mätt**. Layout, sektioner och typografi är
  modellens läsning av bilden — bra utkast, rätta med `inspirime tag`.
- Servern lyssnar bara på `127.0.0.1` och serverar bara biblioteksmappen.
- MIT-licens.
