# ANVI — personal AI voice assistant

A hands-free voice assistant for Windows with a live 3D plexus-orb web UI. It searches the web, controls your PC, writes code, and starts talking while it's still thinking.

**Pipeline:** browser mic → Deepgram speech-to-text → Groq LLM with tools (streamed) → sentence-by-sentence Deepgram text-to-speech → browser

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # then add your GROQ_API_KEY and DEEPGRAM_API_KEY
python main.py
```

Opens http://127.0.0.1:8000 (on Windows you can also double-click `start_anvi.bat`).

## Desktop app (Windows)

`ANVI.exe` gives ANVI its own window and a tray icon instead of a browser tab, and it keeps listening for "ANVI" while hidden in the tray.

```bash
pip install -r requirements.txt pywebview pystray pyinstaller
build_desktop.bat          # -> dist\ANVI\ANVI.exe  (reads the .env in this folder)
```

Or run it without building: `pythonw desktop.py`. Closing the window hides ANVI to the tray; right-click the tray icon for **Wake / sleep**, **Start with Windows** and **Quit**. **Ctrl+Alt+A** wakes it from any app.

## Android app

`mobile/` is a Flutter app with its own brain: it talks to Groq and Deepgram directly, so it works on any network even when the laptop is off, and it controls the PC through ANVI whenever the PC is reachable.

```bash
cd mobile
flutter build apk --release   # -> build\app\outputs\flutter-apk\app-release.apk
```

Install the APK on the phone, open it, and scan the setup code from the PC (📱 button → **Android app** tab). That code carries your API keys and pairing secret, so never share it.

## Using it

- **Say "ANVI"** (or click the orb / press Space) — wake up. It keeps listening, detects when you stop talking, replies, and listens again. "ANVI, what's the time?" wakes it and answers in one go.
- **Say "ANVI"** again (or "bye ANVI", "ANVI go to sleep") — go back to sleep.
- **Click while it talks** — interrupt.
- **T** or the ⌨ button — type instead of speaking.
- **C** — show/hide the code panel · **Esc** — close panel / sleep.

While asleep, ANVI keeps the mic open and sends only short bursts of speech to speech-to-text (Deepgram Nova-3, Indian English) to check for its name; longer conversation nearby is ignored. In a browser this starts after the first click or key press.

## What it can do

| Ask | What happens |
| --- | --- |
| "Gold rate today in India", "petrol price in Hyderabad", "who won yesterday's match" | Web search: reads the top pages and answers with the numbers and source |
| "Latest news", "news about ISRO" | Google News headlines |
| "Weather this evening", "weather in Mumbai tomorrow" | Live forecast (Open-Meteo) |
| "Open WhatsApp / Chrome / VS Code / settings", "close Spotify" | Opens any Start-menu app, closes running apps gracefully |
| "Play Believer on YouTube", "pause", "next song" | YouTube playback and media keys |
| "Volume 30", "mute", "volume up" | System volume |
| "Take a screenshot" | Saved to `Pictures\ANVI Screenshots` |
| "Battery level?", "how much RAM am I using?" | Battery, CPU, RAM, disk |
| "Find my resume", "open the Downloads folder" | Searches Desktop/Documents/Downloads/Pictures/Music/Videos |
| "Lock my PC", "shut down", "restart", "cancel shutdown" | Shutdown/restart/sleep ask for a spoken "yes" first, then wait 30s |
| "Write a Python script that…" | Code panel with syntax highlighting and copy button, saved to `generated/`, not spoken |

## On your phone

ANVI keeps running on the PC; your phone becomes a remote mic, speaker and screen for it (PC actions still happen on the PC).

1. Click the **📱** button on the PC.
2. **Same network:** the PC also serves `https://<PC-IP>:8443`. Put the phone on the same Wi-Fi, or, on college/office Wi-Fi that blocks device-to-device traffic, turn on the **phone's hotspot** and connect the PC to it. Scan the QR code and accept the certificate warning (Advanced → Proceed). Allow Python on private networks if Windows asks.
3. **Anywhere:** install [Tailscale](https://tailscale.com) on the PC and phone (same account), run `tailscale serve --bg 8000` on the PC, and put the `https://….ts.net` address in `.env` as `ANVI_PUBLIC_URL`. The 📱 dialog then shows a QR code that works on any network with no warning.
4. In the phone browser menu, **Add to Home screen** to open ANVI like an app.

Only paired devices can use ANVI: the QR code carries a secret that is stored as a cookie on the phone. Delete `.anvi\pairing_secret` and restart to unpair every device. On phones the always-on wake word is off (tap the orb to talk) and the screen stays on while ANVI is awake.

## Web search

Works without any key by scraping Bing → DuckDuckGo → Brave → Google News, skipping any engine that blocks or returns irrelevant results. For the most reliable results add a free key to `.env`:

- `TAVILY_API_KEY` — https://tavily.com (1,000 free searches/month, built for AI assistants)
- or `BRAVE_API_KEY` — https://brave.com/search/api

## Project layout

```
main.py          FastAPI app: streaming chat, tool calling, speech, /api/* endpoints
search.py        web search + page reading, news
pc.py            Windows PC control (apps, media, volume, screenshots, files, power)
weather.py       Open-Meteo weather
net.py           shared HTTP session (Windows certificate store)
phone.py         phone access: LAN HTTPS certificate, QR pairing secret
desktop.py       Windows desktop app: window, tray icon, hotkey, start with Windows
mobile/          Flutter Android app (own brain + PC control through /api/tool)
web/index.html   UI shell
web/style.css    styling
web/script.js    wake word, voice activity detection, streamed playback, code panel, plexus orb
```

## Notes

- The API only accepts requests from ANVI's own page (Host/Origin check), so other websites can't drive your PC through it.
- Groq's free tier allows ~8k tokens/minute per model; ANVI keeps prompts small and falls back to `openai/gpt-oss-20b` when rate-limited.
- Works behind antivirus HTTPS scanning (Avast/AVG Web Shield) by using the Windows certificate store.
- Weather location comes from `ANVI_LOCATION` in `.env`, or is detected from your IP.
