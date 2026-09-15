// Karen web client
// States: sleep -> listening -> thinking -> speaking -> listening ...
// Hands-free: once woken, the mic stays open and a small voice-activity
// detector decides when you started and stopped talking.

const $ = (id) => document.getElementById(id);
const canvas = $("orb");
const ctx = canvas.getContext("2d");
const statusEl = $("status");
const hintEl = $("hint");
const youText = $("youText");
const transcriptEl = $("transcript");
const replyEl = $("replyText");
const composer = $("composer");
const textInput = $("textInput");

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let state = "sleep";
let awake = false; // hands-free conversation mode on/off
let turnId = 0;
let abortCtl = null;
let errorTimer = null;
let statusDetail = ""; // e.g. "searching: gold rate today" while thinking

const IS_TOUCH = matchMedia("(pointer: coarse)").matches;
let passive = false; // asleep but listening for "Karen"

const STATUS = {
  sleep: ["say “Karen” to wake", IS_TOUCH ? "or tap the orb" : "or click the orb · space"],
  listening: ["listening...", "speak naturally · say “Karen” to sleep"],
  thinking: ["thinking...", "one moment"],
  speaking: ["speaking...", "tap to interrupt"],
};

function setState(next) {
  state = next;
  if (errorTimer) return; // keep an error visible until its timer clears
  renderStatus();
}

function renderStatus() {
  let [s, h] = STATUS[state];
  if (state === "sleep" && !passive) {
    [s, h] = audioCtx && audioCtx.state === "running"
      ? ["tap to wake", IS_TOUCH ? "tap the orb" : "click the orb or press space"]
      : ["tap anywhere to start", "one tap lets Karen listen for her name"];
  }
  statusEl.textContent = state === "thinking" && statusDetail ? statusDetail.replace(/\.+$/, "") + "..." : s;
  statusEl.className = "status" + (state === "sleep" ? " muted" : "");
  hintEl.textContent = h;
}

function showError(msg) {
  console.error("Karen:", msg);
  statusEl.textContent = msg.length > 90 ? msg.slice(0, 90) + "…" : msg;
  statusEl.className = "status error";
  clearTimeout(errorTimer);
  errorTimer = setTimeout(() => {
    errorTimer = null;
    renderStatus();
  }, 4000);
}

function showYou(text) {
  youText.textContent = text;
  transcriptEl.classList.toggle("empty", !text);
}

const stepsEl = $("steps");

function clearSteps() {
  stepsEl.innerHTML = "";
  stepsEl.hidden = true;
}

function addStep(step) {
  const li = document.createElement("li");
  li.className = step.outcome === "done" ? "ok" : step.outcome === "waiting for your OK" ? "wait" : "bad";
  li.textContent = step.outcome === "done" ? step.text : `${step.text} — ${step.outcome}`;
  stepsEl.appendChild(li);
  while (stepsEl.children.length > 6) stepsEl.firstChild.remove();
  stepsEl.hidden = false;
}

function showReply(text) {
  replyEl.textContent = text || "";
  replyEl.classList.toggle("show", !!text);
}

// ---------------------------------------------------------------------------
// Audio plumbing
// ---------------------------------------------------------------------------
let audioCtx = null;
let micStream = null;
let micAnalyser = null;
let outAnalyser = null;
const levelBuf = new Float32Array(1024);

function ensureAudioCtx() {
  if (!audioCtx) {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    outAnalyser = audioCtx.createAnalyser();
    outAnalyser.fftSize = 1024;
    outAnalyser.connect(audioCtx.destination);
    const unlock = audioCtx.createBufferSource();
    unlock.buffer = audioCtx.createBuffer(1, 1, 22050);
    unlock.connect(audioCtx.destination);
    unlock.start(0);
  }
  if (audioCtx.state === "suspended") audioCtx.resume();
  return audioCtx;
}

function rms(analyser) {
  if (!analyser) return 0;
  analyser.getFloatTimeDomainData(levelBuf);
  let sum = 0;
  for (let i = 0; i < levelBuf.length; i++) sum += levelBuf[i] * levelBuf[i];
  return Math.sqrt(sum / levelBuf.length);
}

async function openMic() {
  if (micStream) return true;
  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch (err) {
    showError(err.name === "NotAllowedError" ? "microphone blocked — allow mic access" : "no microphone found");
    return false;
  }
  const ac = ensureAudioCtx();
  micAnalyser = ac.createAnalyser();
  micAnalyser.fftSize = 1024;
  ac.createMediaStreamSource(micStream).connect(micAnalyser);
  return true;
}

function closeMic() {
  stopRecorder(true);
  if (micStream) micStream.getTracks().forEach((t) => t.stop());
  micStream = null;
  micAnalyser = null;
}

// ---------------------------------------------------------------------------
// Recording + voice activity detection
// ---------------------------------------------------------------------------
const MIME = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"].find(
  (m) => window.MediaRecorder && MediaRecorder.isTypeSupported(m)
) || "";

let recorder = null;
const vad = { floor: 0.004, voicedMs: 0, silenceMs: 0, totalVoicedMs: 0, speaking: false, startedAt: 0, recStart: 0 };

function startRecorder() {
  if (!micStream) return;
  const rec = new MediaRecorder(micStream, MIME ? { mimeType: MIME } : undefined);
  const chunks = [];
  rec.discard = false;
  rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);
  rec.onstop = () => {
    if (rec.discard) {
      // silence timeout or noise: just start a fresh take
      const listeningNow = awake ? state === "listening" : state === "sleep" && passive && !wakeChecking;
      if (listeningNow && !recorder && micStream) startRecorder();
      return;
    }
    const blob = new Blob(chunks, { type: rec.mimeType || MIME || "audio/webm" });
    if (!awake) return checkWakeWord(blob);
    // phones play audio through the quiet earpiece while the mic is open
    if (IS_TOUCH) closeMic();
    handleUtterance(blob);
  };
  rec.start(200);
  recorder = rec;
  Object.assign(vad, { voicedMs: 0, silenceMs: 0, totalVoicedMs: 0, speaking: false, recStart: performance.now() });
}

function stopRecorder(discard) {
  if (recorder && recorder.state !== "inactive") {
    recorder.discard = discard;
    recorder.stop();
  }
  recorder = null;
}

async function beginListening() {
  if (!awake) return setState("sleep");
  setState("listening");
  if (!micStream && !(await openMic())) return goToSleep();
  if (awake && state === "listening" && !recorder) startRecorder();
}

// runs every 40ms (keeps working when the tab is in the background)
setInterval(() => {
  const asleepListening = !awake && state === "sleep" && passive;
  if (!(state === "listening" || asleepListening) || !recorder || !micAnalyser) return;
  const dt = 40;
  const level = rms(micAnalyser);
  const now = performance.now();

  if (!vad.speaking) vad.floor = vad.floor * 0.97 + Math.min(level, vad.floor * 3) * 0.03;
  const threshold = Math.max(0.012, vad.floor * 3);

  if (level > threshold) {
    vad.voicedMs += dt;
    vad.silenceMs = 0;
    if (vad.speaking) vad.totalVoicedMs += dt;
    if (!vad.speaking && vad.voicedMs >= 160) {
      vad.speaking = true;
      vad.startedAt = now;
      vad.totalVoicedMs = vad.voicedMs;
    }
  } else {
    vad.voicedMs = Math.max(0, vad.voicedMs - dt / 2);
    if (vad.speaking) vad.silenceMs += dt;
  }

  // asleep: only short phrases can be "Karen ..." (long talk nearby is ignored)
  const endSilence = asleepListening ? 600 : 900;
  const maxLength = asleepListening ? 4500 : 25000;
  if (vad.speaking && (vad.silenceMs >= endSilence || now - vad.startedAt > maxLength)) {
    const tooShort = vad.totalVoicedMs < 250; // a click or bump
    const tooLong = asleepListening && now - vad.startedAt > maxLength;
    if (tooShort || tooLong) {
      stopRecorder(true);
    } else {
      if (!asleepListening) setState("thinking");
      stopRecorder(false);
    }
  } else if (!vad.speaking && vad.voicedMs === 0 && now - vad.recStart > (asleepListening ? 3000 : 12000)) {
    stopRecorder(true); // nothing said for a while; restart to keep takes small
  }
}, 40);

// ---------------------------------------------------------------------------
// Conversation turn
// ---------------------------------------------------------------------------
async function handleUtterance(blob) {
  const id = ++turnId;
  setState("thinking");
  abortCtl = new AbortController();

  try {
    const form = new FormData();
    form.append("audio", blob, "speech.webm");
    form.append("mime_type", blob.type);
    const res = await fetch("/api/transcribe", { method: "POST", body: form, signal: abortCtl.signal });
    const data = await res.json();
    if (id !== turnId) return;
    if (!res.ok) throw new Error(res.status === 401 ? "this phone isn't paired — scan the QR code on your PC" : data.error || "transcription failed");

    if (!data.transcript) return beginListening(); // noise, not speech
    if (isSleepCommand(data.transcript)) {
      showYou(data.transcript);
      showReply("");
      return goToSleep();
    }
    await converse(data.transcript, id);
  } catch (err) {
    if (err.name === "AbortError" || id !== turnId) return;
    showError(err.message || "connection lost — is main.py running?");
    beginListening();
  }
}

async function sendText(text) {
  text = text.trim();
  if (!text) return;
  ensureAudioCtx();
  cancelTurn();
  stopRecorder(true);
  if (IS_TOUCH) closeMic();
  const id = ++turnId;
  setState("thinking");
  try {
    await converse(text, id);
  } catch (err) {
    if (err.name === "AbortError" || id !== turnId) return;
    showError(err.message || "connection lost — is main.py running?");
    awake ? beginListening() : backToSleepListening();
  }
}

async function converse(text, id) {
  showYou(text);
  showReply("");
  clearSteps();
  statusDetail = "";
  setState("thinking");
  abortCtl = abortCtl && !abortCtl.signal.aborted ? abortCtl : new AbortController();

  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
    signal: abortCtl.signal,
  });
  if (!res.ok) {
    let msg = "Karen could not answer";
    try { msg = (await res.json()).error || msg; } catch (_) {}
    throw new Error(msg);
  }

  // the reply streams in as one JSON event per line
  const queue = (currentQueue = new SpeechQueue(id));
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffered = "";
  let failure = null;

  const handle = (ev) => {
    if (ev.t === "caption") showReply(ev.text);
    else if (ev.t === "audio") queue.add(ev.data);
    else if (ev.t === "code") showCode(ev.blocks);
    else if (ev.t === "step") addStep(ev);
    else if (ev.t === "error") failure = ev.error;
    else if (ev.t === "status") {
      statusDetail = ev.text;
      if (state === "thinking") renderStatus();
    }
  };

  for (;;) {
    const { value, done } = await reader.read();
    if (id !== turnId) {
      reader.cancel().catch(() => {});
      return;
    }
    if (done) break;
    buffered += decoder.decode(value, { stream: true });
    let nl;
    while ((nl = buffered.indexOf("\n")) >= 0) {
      const line = buffered.slice(0, nl).trim();
      buffered = buffered.slice(nl + 1);
      if (line) handle(JSON.parse(line));
    }
  }

  queue.streamDone = true;
  if (failure) {
    queue.stop();
    throw new Error(failure);
  }
  await queue.finished();
  if (id !== turnId) return;
  currentQueue = null;
  awake ? beginListening() : backToSleepListening();
}

// Plays streamed MP3 clips back to back on the AudioContext timeline, so
// Karen starts talking after the first sentence instead of the whole reply.
class SpeechQueue {
  constructor(id) {
    this.id = id;
    this.chain = Promise.resolve();
    this.sources = new Set();
    this.endAt = 0;
    this.scheduled = 0;
    this.blocked = false;
    this.streamDone = false;
  }

  add(b64) {
    this.chain = this.chain.then(async () => {
      if (this.id !== turnId || this.blocked) return;
      const ac = ensureAudioCtx();
      if (ac.state !== "running") {
        // browsers block audio until the page has been clicked once
        await Promise.race([ac.resume(), new Promise((r) => setTimeout(r, 400))]);
        if (ac.state !== "running") {
          this.blocked = true;
          return showError("click the page once to enable Karen's voice");
        }
      }
      let buffer;
      try {
        buffer = await ac.decodeAudioData(Uint8Array.from(atob(b64), (c) => c.charCodeAt(0)).buffer);
      } catch (_) {
        return;
      }
      if (this.id !== turnId) return;

      const src = ac.createBufferSource();
      src.buffer = buffer;
      src.connect(outAnalyser);
      const startAt = Math.max(ac.currentTime + 0.03, this.endAt);
      src.start(startAt);
      this.endAt = startAt + buffer.duration;
      this.scheduled++;
      this.sources.add(src);
      src.onended = () => {
        this.sources.delete(src);
        // a filler ("let me check") finished but the answer is still coming
        if (!this.sources.size && !this.streamDone && this.id === turnId) setState("thinking");
      };
      if (state !== "speaking") setState("speaking");
    });
  }

  async finished() {
    await this.chain;
    if (!this.scheduled || !audioCtx || this.id !== turnId) return;
    const remaining = (this.endAt - audioCtx.currentTime) * 1000;
    if (remaining > 0) await new Promise((r) => setTimeout(r, remaining + 80));
  }

  stop() {
    this.id = -1;
    for (const src of this.sources) {
      src.onended = null;
      try { src.stop(); } catch (_) {}
    }
    this.sources.clear();
  }
}

let currentQueue = null;

function cancelTurn() {
  turnId++;
  if (abortCtl) abortCtl.abort();
  abortCtl = null;
  if (currentQueue) currentQueue.stop();
  currentQueue = null;
}

// ---------------------------------------------------------------------------
// Code panel
// ---------------------------------------------------------------------------
const codePanel = $("codePanel");
const codeTabs = $("codeTabs");
const codeEl = $("codeEl");
const codeMeta = $("codeMeta");
const copyBtn = $("copyBtn");
const codeBtn = $("codeBtn");
let codeBlocks = [];
let activeBlock = 0;

function showCode(blocks) {
  codeBlocks = blocks;
  codeTabs.innerHTML = "";
  blocks.forEach((b, i) => {
    const tab = document.createElement("button");
    tab.textContent = b.filename || `${b.lang || "code"} ${blocks.length > 1 ? i + 1 : ""}`.trim();
    tab.addEventListener("click", () => selectBlock(i));
    codeTabs.appendChild(tab);
  });
  selectBlock(0);
  codeBtn.hidden = false;
  openCode();
}

function selectBlock(i) {
  activeBlock = i;
  const b = codeBlocks[i];
  [...codeTabs.children].forEach((t, j) => t.classList.toggle("active", j === i));

  const lines = b.code.split("\n").length;
  codeMeta.textContent = [b.lang, `${lines} line${lines === 1 ? "" : "s"}`, b.path && `saved → ${b.path}`]
    .filter(Boolean)
    .join("  ·  ");

  if (window.hljs) {
    const res = hljs.getLanguage(b.lang)
      ? hljs.highlight(b.code, { language: b.lang, ignoreIllegals: true })
      : hljs.highlightAuto(b.code);
    codeEl.innerHTML = res.value;
  } else {
    codeEl.textContent = b.code;
  }
  codeEl.parentElement.scrollTop = 0;
  copyBtn.textContent = "copy";
}

const codeIsOpen = () => document.body.classList.contains("code-open");
const openCode = () => codeBlocks.length && document.body.classList.add("code-open");
const closeCode = () => document.body.classList.remove("code-open");

copyBtn.addEventListener("click", async () => {
  const b = codeBlocks[activeBlock];
  if (!b) return;
  try {
    await navigator.clipboard.writeText(b.code);
    copyBtn.textContent = "copied ✓";
  } catch {
    copyBtn.textContent = "copy failed";
  }
  setTimeout(() => (copyBtn.textContent = "copy"), 1600);
});

$("closeCodeBtn").addEventListener("click", closeCode);
codeBtn.addEventListener("click", () => (codeIsOpen() ? closeCode() : openCode()));

// ---------------------------------------------------------------------------
// Wake word: say "Karen" to wake up, say "Karen" again to go to sleep
// ---------------------------------------------------------------------------
// While asleep the browser's built-in speech recognizer listens for the name
// (Edge/Chrome only). While awake, the Deepgram transcript is checked instead.
// "Karen" as speech-to-text may spell it (Karen, Caren, Karan, Karin, Keren...)
const WAKE_RE = /\b[kc](?:a|e|ae|ai)r+(?:e|a|i|y)n+\b/;
const SLEEP_WORDS = new Set(
  "hey hi ok okay go to sleep bye by goodbye good night stop thanks thank you that s all please now shut down standby pause the a".split(" ")
);

const normalize = (text) => ` ${text.toLowerCase().replace(/[^a-z\s]/g, " ").replace(/\s+/g, " ")} `;
const hasWakeWord = (text) => WAKE_RE.test(normalize(text));

// "Karen", "bye Karen", "Karen go to sleep" -> true;  "Karen what's the time" -> false
function isSleepCommand(text) {
  const n = normalize(text);
  if (!WAKE_RE.test(n)) return false;
  const rest = n.replace(new RegExp(WAKE_RE.source, "g"), " ").trim().split(" ").filter((w) => w && !SLEEP_WORDS.has(w));
  return rest.length === 0;
}

// While asleep the mic stays open and each short phrase goes to the same
// speech-to-text as normal questions; if it contains "Karen", Karen wakes up
// (and "Karen, what's the time?" is answered straight away).
let wakeChecking = false;
let passiveToken = 0;

function afterWakeWord(text) {
  const m = new RegExp(WAKE_RE.source, "i").exec(text);
  if (!m) return "";
  const rest = text.slice(m.index + m[0].length).replace(/^[\s,.!?:;-]+/, "").trim();
  return rest.split(/\s+/).filter(Boolean).length >= 2 ? rest : "";
}

async function checkWakeWord(blob) {
  wakeChecking = true;
  try {
    const form = new FormData();
    form.append("audio", blob, "wake.webm");
    form.append("mime_type", blob.type);
    form.append("purpose", "wake");
    const res = await fetch("/api/transcribe", { method: "POST", body: form });
    if (res.status === 401) {
      passive = false;
      return showError("this phone isn't paired — scan the QR code on your PC");
    }
    const text = res.ok ? (await res.json()).transcript || "" : "";
    // "Karen, go to sleep" while already asleep: nothing to do
    if (!awake && state === "sleep" && hasWakeWord(text) && !(isSleepCommand(text) && afterWakeWord(text))) {
      wakeChecking = false;
      return wakeUp(afterWakeWord(text));
    }
  } catch (_) {
    // offline for a moment: keep listening
  } finally {
    wakeChecking = false;
  }
  if (!awake && state === "sleep" && passive && !recorder && micStream) startRecorder();
}

async function startPassive() {
  const token = ++passiveToken;
  if (awake) return;
  if (!audioCtx || audioCtx.state !== "running") {
    passive = false; // needs one tap first
    return renderStatus();
  }
  const ok = await openMic();
  if (token !== passiveToken || awake) return;
  passive = ok;
  renderStatus();
  if (ok && state === "sleep" && !recorder && !wakeChecking) startRecorder();
}

function backToSleepListening() {
  setState("sleep");
  startPassive();
}

function chime(up) {
  const ac = audioCtx;
  if (!ac || ac.state !== "running") return;
  const t = ac.currentTime;
  const osc = ac.createOscillator();
  const gain = ac.createGain();
  osc.type = "sine";
  osc.frequency.setValueAtTime(up ? 620 : 900, t);
  osc.frequency.exponentialRampToValueAtTime(up ? 980 : 520, t + 0.18);
  gain.gain.setValueAtTime(0.0001, t);
  gain.gain.exponentialRampToValueAtTime(0.07, t + 0.03);
  gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.28);
  osc.connect(gain).connect(ac.destination);
  osc.start(t);
  osc.stop(t + 0.3);
}

let wakeLock = null;

async function keepScreenOn(on) {
  try {
    if (on && !wakeLock && navigator.wakeLock) {
      wakeLock = await navigator.wakeLock.request("screen");
      wakeLock.addEventListener("release", () => (wakeLock = null));
    } else if (!on && wakeLock) {
      await wakeLock.release();
      wakeLock = null;
    }
  } catch (_) {}
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && awake) keepScreenOn(true);
});

async function wakeUp(request = "") {
  if (awake) return;
  passiveToken++;
  passive = false;
  stopRecorder(true);
  ensureAudioCtx();
  if (!(await openMic())) return startPassive();
  awake = true;
  keepScreenOn(true);
  chime(true);
  if (request) return sendText(request); // "Karen, what's the time?"
  setState("listening");
  setTimeout(() => awake && state === "listening" && !recorder && beginListening(), 300); // skip the chime
}

function goToSleep() {
  cancelTurn();
  awake = false;
  keepScreenOn(false);
  stopRecorder(true);
  setState("sleep");
  chime(false);
  startPassive(); // keep listening for "Karen"
}

// ---------------------------------------------------------------------------
// Phone pairing (button only shows on the PC itself)
// ---------------------------------------------------------------------------
const phoneBtn = $("phoneBtn");
const phoneModal = $("phoneModal");
const IS_PC_PAGE = ["127.0.0.1", "localhost"].includes(location.hostname);
phoneBtn.hidden = !IS_PC_PAGE;

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const el = document.createElement("script");
    el.src = src;
    el.onload = resolve;
    el.onerror = reject;
    document.head.appendChild(el);
  });
}

function drawQr(el, text) {
  el.innerHTML = "";
  if (!text) return;
  if (window.QRCode) new QRCode(el, { text, width: 400, height: 400, correctLevel: QRCode.CorrectLevel.M });
  else el.textContent = "QR library couldn't load (offline?) — open the address below on your phone.";
}

function showPhoneTab(mode) {
  phoneModal.querySelectorAll(".modal-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  phoneModal.querySelectorAll(".modal-body").forEach((p) => (p.hidden = p.dataset.panel !== mode));
}

async function openPhoneModal() {
  phoneModal.hidden = false;
  try {
    const [info, appConfig] = await Promise.all([
      fetch("/api/phone").then((r) => r.json()),
      fetch("/api/app-config").then((r) => r.json()),
      window.QRCode ? null : loadScript("https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js").catch(() => null),
    ]);
    if (!info.enabled) {
      $("addrLan").textContent = "Phone access is off (ANVI_PHONE=0 in .env).";
      return;
    }
    drawQr($("qrApp"), appConfig.qr);
    drawQr($("qrLan"), info.lan.pair_url);
    $("addrLan").textContent = info.lan.address;
    const pub = info.public;
    $("publicSetup").hidden = !!pub;
    $("publicReady").hidden = !pub;
    drawQr($("qrPublic"), pub && pub.pair_url);
    $("addrPublic").textContent = pub ? pub.address : "";
    showPhoneTab(pub ? "public" : "lan");
  } catch (err) {
    $("addrLan").textContent = "Couldn't get the phone link: " + err.message;
  }
}

phoneBtn.addEventListener("click", openPhoneModal);
$("phoneClose").addEventListener("click", () => (phoneModal.hidden = true));
phoneModal.addEventListener("click", (e) => e.target === phoneModal && (phoneModal.hidden = true));
phoneModal.querySelectorAll(".modal-tabs button").forEach((b) => b.addEventListener("click", () => showPhoneTab(b.dataset.mode)));

// ---------------------------------------------------------------------------
// Controls
// ---------------------------------------------------------------------------
async function toggle() {
  ensureAudioCtx();

  if (state === "speaking" || state === "thinking") {
    // interrupt and listen again
    cancelTurn();
    if (!awake) return backToSleepListening();
    return beginListening();
  }

  if (awake) return goToSleep();
  wakeUp();
}

canvas.addEventListener("click", () => {
  if (composer.classList.contains("open")) closeComposer();
  toggle();
});

function openComposer() {
  composer.classList.add("open");
  textInput.focus();
}

function closeComposer() {
  composer.classList.remove("open");
  textInput.blur();
}

$("keyboardBtn").addEventListener("click", () =>
  composer.classList.contains("open") ? closeComposer() : openComposer()
);

composer.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = textInput.value;
  textInput.value = "";
  sendText(text);
});

window.addEventListener("keydown", (e) => {
  if (!phoneModal.hidden) {
    if (e.key === "Escape") phoneModal.hidden = true;
    return;
  }
  if (e.target === textInput) {
    if (e.key === "Escape") closeComposer();
    return;
  }
  if (e.code === "Space") {
    e.preventDefault();
    toggle();
  } else if (e.key === "t" || e.key === "T" || e.key === "/") {
    e.preventDefault();
    openComposer();
  } else if (e.key === "c" || e.key === "C") {
    codeIsOpen() ? closeCode() : openCode();
  } else if (e.key === "Escape") {
    if (codeIsOpen()) closeCode();
    else if (awake) goToSleep();
  }
});

// audio can only start after a click/keypress; unlock it on the first one
// Browsers only allow audio (and listening) after the first tap or key press.
let started = false;
async function firstInteraction() {
  if (started) return;
  started = true;
  const ac = ensureAudioCtx();
  try { await ac.resume(); } catch (_) {}
  if (!awake && state === "sleep") startPassive();
}
["pointerdown", "keydown"].forEach((ev) => window.addEventListener(ev, firstInteraction, { once: true }));

// The desktop app allows audio without a click, so start listening for "Karen" immediately.
if (new URLSearchParams(location.search).get("app") === "desktop") firstInteraction();

fetch("/api/health")
  .then((r) => r.json())
  .then((h) => {
    if (!h.groq_key || !h.deepgram_key) showError("API keys missing in .env");
  })
  .catch(() => showError("backend offline — run: python main.py"));

showYou("");
renderStatus();

// ---------------------------------------------------------------------------
// Plexus orb
// ---------------------------------------------------------------------------
const TAU = Math.PI * 2;
const rnd = (a, b) => a + Math.random() * (b - a);

function randomDir() {
  let x, y, z, d;
  do {
    x = rnd(-1, 1); y = rnd(-1, 1); z = rnd(-1, 1);
    d = x * x + y * y + z * z;
  } while (d > 1 || d < 1e-4);
  d = Math.sqrt(d);
  return [x / d, y / d, z / d];
}

// irregular, lumpy outline like a cluster of neurons rather than a perfect ball
function lobe(x, y, z) {
  return 1 + 0.09 * Math.sin(3 * x + 2 * y + 0.7) + 0.07 * Math.cos(4 * z - 2 * x) + 0.05 * Math.sin(5 * y + z);
}

const nodes = [];
function addNode(rMin, rMax, pow, kind) {
  const [x, y, z] = randomDir();
  const r = (rMin + Math.pow(Math.random(), pow) * (rMax - rMin)) * lobe(x, y, z);
  nodes.push({
    x: x * r, y: y * r, z: z * r, r, kind,
    ph: rnd(0, TAU), sp: rnd(0.6, 1.8), amp: rnd(0.4, 1),
    size: kind === "dust" ? rnd(0.5, 1.1) : rnd(0.7, 1.9),
    hot: Math.random() < 0.08,
  });
}
for (let i = 0; i < 380; i++) addNode(0.84, 1.0, 1, "shell");
for (let i = 0; i < 360; i++) addNode(0.03, 0.84, 1.5, "inner");
for (let i = 0; i < 160; i++) addNode(1.02, 1.35, 1.6, "dust");

const edges = []; // [i, j, weight]
(function buildEdges() {
  const linked = new Set();
  const link = (i, j, w) => {
    const key = i < j ? i * 4096 + j : j * 4096 + i;
    if (i === j || linked.has(key)) return;
    linked.add(key);
    edges.push([i, j, w]);
  };
  const core = nodes.map((n, i) => i).filter((i) => nodes[i].kind !== "dust");
  for (const i of core) {
    const a = nodes[i];
    const near = [];
    for (const j of core) {
      if (i === j) continue;
      const b = nodes[j];
      const d = (a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2;
      if (d < 0.16) near.push([d, j]);
    }
    near.sort((p, q) => p[0] - q[0]);
    const k = a.kind === "shell" ? 4 : 3;
    near.slice(0, k).forEach(([, j]) => link(i, j, 1));
  }
  // long spokes from the surface into the bright core
  const hubs = core.filter((i) => nodes[i].r < 0.38);
  const shell = core.filter((i) => nodes[i].kind === "shell");
  for (let s = 0; s < 320; s++) {
    link(shell[(Math.random() * shell.length) | 0], hubs[(Math.random() * hubs.length) | 0], 0.55);
  }
  // a few long chords across the sphere
  for (let s = 0; s < 90; s++) {
    link(shell[(Math.random() * shell.length) | 0], shell[(Math.random() * shell.length) | 0], 0.35);
  }
})();

const N = nodes.length;
const sx = new Float32Array(N);
const sy = new Float32Array(N);
const sd = new Float32Array(N);
const BUCKETS = 10;
const edgeBucket = new Int8Array(edges.length);

let W = 0, H = 0, DPR = 1;
function resize() {
  DPR = Math.min(window.devicePixelRatio || 1, 2);
  W = window.innerWidth;
  H = window.innerHeight;
  canvas.width = W * DPR;
  canvas.height = H * DPR;
}
resize();
window.addEventListener("resize", resize);

const vis = { scale: 0.9, spin: 0.06, glow: 0.35, jitter: 0.015, line: 0.45, ripple: 0 };
let level = 0;
let rotY = 0;
const orbLayout = { R: 0, cx: 0, cy: 0 };
const dock = $("dock");
let last = performance.now();

function targets(t) {
  const L = level;
  switch (state) {
    case "listening":
      return { scale: 0.94 + L * 0.35, spin: 0.12, glow: 0.55 + L * 1.4, jitter: 0.02 + L * 0.1, line: 0.62 + L * 0.6, ripple: L * 0.5 };
    case "thinking":
      return { scale: 0.86 + 0.035 * Math.sin(t * 5), spin: 0.85, glow: 0.72 + 0.22 * Math.sin(t * 6.5), jitter: 0.05, line: 0.78, ripple: 1 };
    case "speaking":
      return { scale: 1.0 + L * 0.3, spin: 0.2, glow: 0.7 + L * 1.6, jitter: 0.03 + L * 0.12, line: 0.7 + L * 0.7, ripple: L };
    default:
      return { scale: 0.9, spin: 0.06, glow: 0.38, jitter: 0.015, line: 0.45, ripple: 0 };
  }
}

function frame(now) {
  const dt = Math.min(0.05, (now - last) / 1000);
  last = now;
  const t = now / 1000;

  // audio level: fast attack, slow release
  let raw = 0;
  if (state === "listening") raw = Math.min(1, rms(micAnalyser) * 9);
  else if (state === "speaking") raw = Math.min(1, rms(outAnalyser) * 5);
  level += (raw - level) * (raw > level ? 0.45 : 0.08);

  const tg = targets(t);
  for (const k in vis) vis[k] += (tg[k] - vis[k]) * Math.min(1, dt * 5);

  rotY += vis.spin * dt;
  const rotX = 0.32 + Math.sin(t * 0.17) * 0.14;
  const cyA = Math.cos(rotY), syA = Math.sin(rotY);
  const cxA = Math.cos(rotX), sxA = Math.sin(rotX);

  // keep the orb centred in the space the code panel leaves free
  // and vertically between the transcript (top) and the dock (bottom)
  const panel = codePanel.getBoundingClientRect();
  const freeW = W > 800 ? Math.min(W, panel.left) : W;
  const freeH = W > 800 ? H : Math.min(H, panel.top);
  const dockRect = dock.getBoundingClientRect();
  const areaTop = transcriptEl.getBoundingClientRect().bottom + 16;
  const areaBottom = Math.min(freeH, dockRect.height ? dockRect.top : freeH) - 16;
  const targetR = Math.max(60, Math.min(freeW * (W < 600 ? 0.4 : 0.3), (areaBottom - areaTop) * 0.4));
  const ease = orbLayout.R ? Math.min(1, dt * 4) : 1;
  orbLayout.R += (targetR - orbLayout.R) * ease;
  orbLayout.cx += (freeW / 2 - orbLayout.cx) * ease;
  orbLayout.cy += ((areaTop + areaBottom) / 2 - orbLayout.cy) * ease;
  const { R, cx, cy } = orbLayout;
  const cam = 3.4;

  for (let i = 0; i < N; i++) {
    const n = nodes[i];
    const wob = Math.sin(t * n.sp + n.ph) * vis.jitter * n.amp * (n.kind === "dust" ? 3 : 1);
    const wave = vis.ripple * 0.06 * Math.sin(n.r * 11 - t * 7);
    const k = vis.scale * (1 + wob + wave);
    let x = n.x * k, y = n.y * k, z = n.z * k;
    // rotate around Y, then X
    const x1 = x * cyA + z * syA;
    const z1 = -x * syA + z * cyA;
    const y1 = y * cxA - z1 * sxA;
    const z2 = y * sxA + z1 * cxA;
    const p = cam / (cam - z2);
    sx[i] = cx + x1 * p * R;
    sy[i] = cy + y1 * p * R;
    sd[i] = Math.max(0.15, Math.min(1, (z2 + 1.4) / 2.6));
  }

  const g = ctx;
  g.setTransform(DPR, 0, 0, DPR, 0, 0);
  g.globalCompositeOperation = "source-over";
  g.clearRect(0, 0, W, H);
  g.globalCompositeOperation = "lighter";

  // halo
  const halo = g.createRadialGradient(cx, cy, 0, cx, cy, R * 1.6);
  halo.addColorStop(0, `rgba(40,170,230,${0.12 * vis.glow})`);
  halo.addColorStop(1, "rgba(0,0,0,0)");
  g.fillStyle = halo;
  g.fillRect(cx - R * 1.6, cy - R * 1.6, R * 3.2, R * 3.2);

  // edges, batched into alpha buckets
  for (let e = 0; e < edges.length; e++) {
    const [i, j, w] = edges[e];
    const a = vis.line * w * (sd[i] + sd[j]) * 0.5;
    edgeBucket[e] = Math.min(BUCKETS - 1, (a * BUCKETS) | 0);
  }
  g.lineWidth = 0.8;
  for (let b = 1; b < BUCKETS; b++) {
    g.beginPath();
    for (let e = 0; e < edges.length; e++) {
      if (edgeBucket[e] !== b) continue;
      const [i, j] = edges[e];
      g.moveTo(sx[i], sy[i]);
      g.lineTo(sx[j], sy[j]);
    }
    g.strokeStyle = `rgba(80,215,250,${(b / BUCKETS) * 0.8})`;
    g.stroke();
  }

  // nodes
  for (let i = 0; i < N; i++) {
    const n = nodes[i];
    const d = sd[i];
    const twinkle = 0.75 + 0.25 * Math.sin(t * 3 * n.sp + n.ph);
    const s = n.size * (0.6 + d * 0.8) * (n.hot ? 1.5 : 1);
    const a = Math.min(1, d * twinkle * (n.kind === "dust" ? 0.55 : 0.95) * (0.7 + vis.line * 0.5));
    g.fillStyle = n.hot ? `rgba(200,250,255,${a})` : `rgba(90,225,255,${a})`;
    g.fillRect(sx[i] - s / 2, sy[i] - s / 2, s, s);
  }

  // hot core
  const coreR = R * (0.5 + vis.glow * 0.25);
  const core = g.createRadialGradient(cx, cy, 0, cx, cy, coreR);
  core.addColorStop(0, `rgba(255,255,255,${Math.min(1, 0.55 + vis.glow * 0.45)})`);
  core.addColorStop(0.12, `rgba(200,250,255,${Math.min(1, 0.35 * vis.glow + 0.15)})`);
  core.addColorStop(0.4, `rgba(60,200,245,${0.24 * vis.glow})`);
  core.addColorStop(1, "rgba(0,0,0,0)");
  g.fillStyle = core;
  g.beginPath();
  g.arc(cx, cy, coreR, 0, TAU);
  g.fill();

  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
