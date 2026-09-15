# Karen — personal AI agent

A hands-free AI agent for Windows and Android with a live 3D plexus-orb UI. Say "Karen" and ask it to *do* things: run commands, organise files, type into apps, test your internet speed, set reminders, search the web — it works step by step until the job is done, and starts talking while it's still thinking.

**Pipeline:** mic → Deepgram speech-to-text (Nova-3, Indian English) → Groq LLM agent loop with tools (streamed) → sentence-by-sentence Deepgram text-to-speech

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # then add your GROQ_API_KEY and DEEPGRAM_API_KEY
python main.py
```

Opens http://127.0.0.1:8000 (on Windows you can also double-click `start_karen.bat`).

## Desktop app (Windows)

`Karen.exe` gives Karen her own window instead of a browser tab. She runs only while the window is open and listens for "Karen" the whole time; closing the window quits her completely, so nothing runs in the background.

```bash
pip install -r requirements.txt pywebview pyinstaller
build_desktop.bat          # -> dist\Karen\Karen.exe  (reads the .env in this folder)
```

Or run it without building: `pythonw desktop.py`. While she's open, **Ctrl+Alt+A** brings her to the front and wakes her from any app.

## Android app

`mobile/` is a Flutter app with its own agent brain: it talks to Groq and Deepgram directly, so it works on any network even when the laptop is off. Phone actions (calls, SMS, WhatsApp, alarms, timers, maps, speed test) run on the phone; PC actions go to Karen on the PC whenever it's reachable.

```bash
build_android.bat          # -> Karen.apk
```

Install the APK, open it, allow the microphone, and scan the setup code from the PC (📱 button → **Android app** tab). That code carries your API keys and pairing secret, so never share it.

## Using it

- **Say "Karen"** (or click the orb / press Space) — wake up. She keeps listening, detects when you stop talking, replies, and listens again. "Karen, run a speed test" wakes her and does it in one go.
- **Say "Karen"** again (or "bye Karen", "Karen go to sleep") — go back to sleep.
- **Click while she talks** — interrupt.
- **T** or the ⌨ button — type instead of speaking.
- **C** — show/hide the code panel · **Esc** — close panel / sleep.

While a task runs, the steps she takes appear above the reply (✓ done, ? waiting for your OK, ✕ failed).

## What she can do

| Ask | What happens |
| --- | --- |
| "Run a speed test" | Download/upload/ping via Cloudflare |
| "Make a folder called Project on my desktop with a notes file listing today's tasks" | Creates folders and files |
| "Move all PDFs from Downloads into Documents/PDFs", "rename report.docx to final.docx" | Moves, copies, renames (asks before replacing) |
| "Delete old_notes.txt" | Asks first, then moves it to the Recycle Bin |
| "What Wi-Fi am I on and what's my IP?", "which apps use the most RAM?", "git status in my project" | Runs PowerShell and reads the output |
| "Open Notepad and type a leave letter" | Opens the app, focuses it and types |
| "Remind me in 20 minutes to call mom" | Pop-up reminder on the PC |
| "Gold rate today", "latest news about ISRO", "weather in Mumbai tomorrow" | Web search / news / forecast with real numbers |
| "Open WhatsApp", "play Believer on YouTube", "volume 30", "take a screenshot" | Apps, media, volume, screenshots (`Pictures\Karen Screenshots`) |
| "Shut down", "restart" | Asks for a spoken "yes" first, then waits 30 s |
| "Write a Python script that…" | Code panel with copy button, saved to `generated/`, not spoken |
| On the phone: "WhatsApp Rahul I'm running late", "set an alarm for 6:30", "navigate to Charminar" | Prepares the message (you tap send), sets alarms/timers, opens Maps |

### Safety

- Anything destructive or system-changing — deleting, overwriting a file, killing processes, registry/service/network changes, installs, `git push`, shutdown — returns *needs confirmation*; Karen describes exactly what will happen and only does it after you say yes.
- Screenshots, locking the PC and closing apps only happen when your own words ask for them.
- If the model claims it did something without actually calling a tool, the claim is discarded and it's told to act (or say it didn't).
- Only this PC's own page and paired phones can use the API.

## On your phone (browser instead of the app)

1. Click the **📱** button on the PC.
2. **Same network:** the PC also serves `https://<PC-IP>:8443`. Put the phone on the same Wi-Fi, or, on college/office Wi-Fi that blocks device-to-device traffic, turn on the **phone's hotspot** and connect the PC to it. Scan the QR code and accept the certificate warning (Advanced → Proceed). Allow Python on private networks if Windows asks.
3. **Anywhere:** install [Tailscale](https://tailscale.com) on the PC and phone (same account), run `tailscale serve --bg 8000` on the PC, and put the `https://….ts.net` address in `.env` as `ANVI_PUBLIC_URL`.

Only paired devices can use Karen: the QR code carries a secret stored as a cookie on the phone. Delete `.anvi\pairing_secret` and restart to unpair every device.

## Web search

Works without any key by scraping Bing → DuckDuckGo → Brave → Google News, skipping any engine that blocks or returns irrelevant results. For the most reliable results add a free key to `.env`:

- `TAVILY_API_KEY` — https://tavily.com (1,000 free searches/month, built for AI agents)
- or `BRAVE_API_KEY` — https://brave.com/search/api

## Project layout

```
main.py          FastAPI app: agent loop, tool calling, confirmations, speech, /api/* endpoints
agent.py         agent tools: PowerShell, files/folders, keyboard/windows, speed test, reminders, clipboard
pc.py            apps, media, volume, screenshots, file search, power
search.py        web search + page reading, news
weather.py       Open-Meteo weather
net.py           shared HTTP session (Windows certificate store)
phone.py         phone access: LAN HTTPS certificate, QR pairing secret
desktop.py       Windows desktop app: window, microphone permission, hotkey
mobile/          Flutter Android app (own agent brain + phone actions + PC tools via /api/tool)
web/             UI: wake word, voice activity detection, streamed playback, step list, code panel, orb
```

## Notes

- Groq's free tier allows ~8k tokens/minute per model. Karen keeps prompts small, shortens old tool output during long tasks, switches between `openai/gpt-oss-120b` and `openai/gpt-oss-20b`, and waits a few seconds when both are busy. Heavy agent use is smoother on Groq's paid tier.
- Settings keep their original `ANVI_*` names in `.env` so existing setups keep working.
- Works behind antivirus HTTPS scanning (Avast/AVG Web Shield) by using the Windows certificate store.
- Weather location comes from `ANVI_LOCATION` in `.env`, or is detected from your IP.
