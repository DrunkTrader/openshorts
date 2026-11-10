// Waveform peaks computed in the browser from the clip that is already served:
// no backend endpoint. Decoding is capped (size, duration) and serialised so a
// page of cards never holds more than one decoded clip in memory.
const MAX_BYTES = 25 * 1024 * 1024;
const MAX_SECONDS = 180;
const cache = new Map();
let chain = Promise.resolve();

async function compute(url, bars) {
  const res = await fetch(url);
  if (!res.ok) return null;
  const len = Number(res.headers.get('content-length')) || 0;
  if (len > MAX_BYTES) return null;
  const buf = await res.arrayBuffer();
  if (buf.byteLength > MAX_BYTES) return null;
  const Ctx = window.OfflineAudioContext || window.webkitOfflineAudioContext;
  if (!Ctx) return null;
  const audio = await new Ctx(1, 1, 44100).decodeAudioData(buf);
  if (audio.duration > MAX_SECONDS) return null;
  const data = audio.getChannelData(0);
  const step = Math.max(1, Math.floor(data.length / bars));
  const out = new Float32Array(bars);
  let top = 0;
  for (let i = 0; i < bars; i++) {
    let m = 0;
    for (let j = i * step; j < Math.min(data.length, (i + 1) * step); j++) {
      const v = Math.abs(data[j]);
      if (v > m) m = v;
    }
    out[i] = m;
    if (m > top) top = m;
  }
  if (top > 0) for (let i = 0; i < bars; i++) out[i] /= top;
  return out;
}

// Resolves to a Float32Array of normalised peaks, or null (no audio track,
// undecodable codec, too big, cross-origin without CORS): callers draw the
// scene strip alone.
export function loadPeaks(url, bars = 96) {
  if (!url) return Promise.resolve(null);
  const key = `${url}|${bars}`;
  if (!cache.has(key)) {
    const p = chain.then(() => compute(url, bars)).catch(() => null);
    chain = p.then(() => undefined);
    cache.set(key, p);
  }
  return cache.get(key);
}
