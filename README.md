# ANVI — personal AI voice assistant

A hands-free voice assistant with a live 3D plexus-orb web UI.

**Pipeline:** browser mic → Deepgram speech-to-text → Groq LLM (with a real weather tool via Open-Meteo) → Deepgram text-to-speech → browser

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # then add your GROQ_API_KEY and DEEPGRAM_API_KEY
python main.py
```

Opens http://127.0.0.1:8000 (on Windows you can also double-click `start_anvi.bat`).

## Using it

- **Click the orb / press Space** — wake ANVI. It keeps listening, detects when you stop talking, replies, and listens again.
- **Click while it talks** — interrupt.
- **Click again / Esc** — sleep (mic off).
- **T** or the ⌨ button — type instead of speaking.

## Project layout

```
main.py          FastAPI backend: /api/transcribe, /api/chat, /api/reset, serves web/
web/index.html   UI shell
web/style.css    styling
web/script.js    voice activity detection, conversation loop, plexus orb renderer
```

## Notes

- Works behind antivirus HTTPS scanning (Avast/AVG Web Shield) by using the Windows certificate store.
- Weather location comes from `ANVI_LOCATION` in `.env`, or is detected from your IP.
