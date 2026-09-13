// ANVI web client
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

const STATUS = {
  sleep: ["tap to wake", "click the orb or press space"],
  listening: ["listening...", "speak naturally · space to pause"],
  thinking: ["thinking...", "one moment"],
  speaking: ["speaking...", "tap to interrupt"],
};

function setState(next) {
  state = next;
  if (errorTimer) return; // keep an error visible until its timer clears
  renderStatus();
}

function renderStatus() {
  const [s, h] = STATUS[state];
  statusEl.textContent = s;
  statusEl.className = "status" + (state === "sleep" ? " muted" : "");
  hintEl.textContent = h;
}

function showError(msg) {
  console.error("ANVI:", msg);
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
let currentSource = null;
const levelBuf = new Float32Array(1024);

function ensureAudioCtx() {
  if (!audioCtx) {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    outAnalyser = audioCtx.createAnalyser();
    outAnalyser.fftSize = 1024;
    outAnalyser.connect(audioCtx.destination);
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
const vad = { floor: 0.004, voicedMs: 0, silenceMs: 0, speaking: false, startedAt: 0, recStart: 0 };

function startRecorder() {
  if (!micStream) return;
  const rec = new MediaRecorder(micStream, MIME ? { mimeType: MIME } : undefined);
  const chunks = [];
  rec.discard = false;
  rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);
  rec.onstop = () => {
    if (rec.discard) {
      // silence timeout: just start a fresh take
      if (awake && state === "listening" && !recorder) startRecorder();
      return;
    }
    handleUtterance(new Blob(chunks, { type: rec.mimeType || MIME || "audio/webm" }));
  };
  rec.start(200);
  recorder = rec;
  Object.assign(vad, { voicedMs: 0, silenceMs: 0, speaking: false, recStart: performance.now() });
}

function stopRecorder(discard) {
  if (recorder && recorder.state !== "inactive") {
    recorder.discard = discard;
    recorder.stop();
  }
  recorder = null;
}

function beginListening() {
  if (!awake) return setState("sleep");
  setState("listening");
  startRecorder();
}

// runs every 40ms (keeps working when the tab is in the background)
setInterval(() => {
  if (state !== "listening" || !recorder || !micAnalyser) return;
  const dt = 40;
  const level = rms(micAnalyser);
  const now = performance.now();

  if (!vad.speaking) vad.floor = vad.floor * 0.97 + Math.min(level, vad.floor * 3) * 0.03;
  const threshold = Math.max(0.012, vad.floor * 3);

  if (level > threshold) {
    vad.voicedMs += dt;
    vad.silenceMs = 0;
    if (!vad.speaking && vad.voicedMs >= 160) {
      vad.speaking = true;
      vad.startedAt = now;
    }
  } else {
    vad.voicedMs = Math.max(0, vad.voicedMs - dt / 2);
    if (vad.speaking) vad.silenceMs += dt;
  }

  if (vad.speaking && (vad.silenceMs >= 900 || now - vad.startedAt > 25000)) {
    setState("thinking");
    stopRecorder(false);
  } else if (!vad.speaking && now - vad.recStart > 12000) {
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
    if (!res.ok) throw new Error(data.error || "transcription failed");

    if (!data.transcript) return beginListening(); // noise, not speech
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
  const id = ++turnId;
  setState("thinking");
  try {
    await converse(text, id);
  } catch (err) {
    if (err.name === "AbortError" || id !== turnId) return;
    showError(err.message || "connection lost — is main.py running?");
    awake ? beginListening() : setState("sleep");
  }
}

async function converse(text, id) {
  showYou(text);
  showReply("");
  setState("thinking");
  abortCtl = abortCtl && !abortCtl.signal.aborted ? abortCtl : new AbortController();

  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
    signal: abortCtl.signal,
  });
  const data = await res.json();
  if (id !== turnId) return;
  if (!res.ok) throw new Error(data.error || "ANVI could not answer");

  showReply(data.reply);
  if (data.audio) await speak(data.audio, id);
  if (id !== turnId) return;
  awake ? beginListening() : setState("sleep");
}

async function speak(b64, id) {
  const ac = ensureAudioCtx();
  const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  const buffer = await ac.decodeAudioData(bytes.buffer);
  if (id !== turnId) return;

  setState("speaking");
  await new Promise((resolve) => {
    const src = ac.createBufferSource();
    src.buffer = buffer;
    src.connect(outAnalyser);
    src.onended = resolve;
    currentSource = src;
    src.start();
  });
  currentSource = null;
}

function cancelTurn() {
  turnId++;
  if (abortCtl) abortCtl.abort();
  abortCtl = null;
  if (currentSource) {
    currentSource.onended = null;
    try { currentSource.stop(); } catch (_) {}
    currentSource = null;
  }
}

// ---------------------------------------------------------------------------
// Controls
// ---------------------------------------------------------------------------
async function toggle() {
  ensureAudioCtx();

  if (state === "speaking" || state === "thinking") {
    // interrupt and listen again
    cancelTurn();
    if (!awake) return setState("sleep");
    if (!(await openMic())) return;
    return beginListening();
  }

  if (awake) {
    awake = false;
    closeMic();
    return setState("sleep");
  }

  if (!(await openMic())) return;
  awake = true;
  beginListening();
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
  } else if (e.key === "Escape" && awake) {
    cancelTurn();
    awake = false;
    closeMic();
    setState("sleep");
  }
});

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
let last = performance.now();

function targets(t) {
  const L = level;
  switch (state) {
    case "listening":
      return { scale: 0.94 + L * 0.35, spin: 0.12, glow: 0.55 + L * 1.4, jitter: 0.02 + L * 0.1, line: 0.62 + L * 0.6, ripple: L * 0.5 };
    case "thinking":
      return { scale: 0.86 + 0.035 * Math.sin(t * 5), spin: 0.85, glow: 0.72 + 0.22 * Math.sin(t * 6.5), jitter: 0.05, line: 0.78, ripple: 1 };
    case "speaking":
      return { scale: 1.0 + L * 0.45, spin: 0.2, glow: 0.7 + L * 1.6, jitter: 0.03 + L * 0.12, line: 0.7 + L * 0.7, ripple: L };
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

  const R = Math.min(W, H) * 0.3;
  const cx = W / 2;
  const cy = H * 0.47;
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
