// Pure: where is a job in the pipeline? Derived from /api/status (status,
// queue, logs) with the markers the backend really prints (main.py prints,
// transcribe_backends progress, app.py lifecycle lines). Self-host sees the raw
// lines, cloud sees log_view.friendly_logs, so every rule matches both forms.
export const STAGES = [
  { id: 'queued', label: 'Queued' },
  { id: 'transcribing', label: 'Transcribing' },
  { id: 'scoring', label: 'Scoring' },
  { id: 'rendering', label: 'Rendering' },
  { id: 'done', label: 'Done' },
];

// Highest matching rule wins, so out-of-order lines (transcription runs while
// the video still downloads) never move the stage backwards.
export const MARKERS = [
  // main.py: "🔥 Found N clips!", "Processing Clip N", "Clip N framed|ready",
  // "Choosing a layout", app.py reads CLIP_READY lines.
  { stage: 'rendering', re: /Found \d+ (?:viral )?clips|Processing Clip \d+|Clip \d+ (?:framed|ready)|Choosing (?:a|the) layout|CLIP_READY|ranges: \d+ pieces?|\d+\/\d+ pieces downloaded|piece\(s\) missing|Using the clips Gemini picked/ },
  // main.py "Analyzing with Gemini|local LLM", "Built N scoring window(s)",
  // "Silent video — analyzing with Gemini vision".
  { stage: 'scoring', re: /Analyzing with (?:Gemini|local LLM)|Finding the best moments|Built \d+ scoring window|Silent video|finding moments in the picture/ },
  // Download + ASR: "Download attempt", yt-dlp "[download] N% of", "Video
  // downloaded", "Audio ready ahead of the video", "Transcribing… N%",
  // "Transcribing video|audio", "Detected language", "Waiting for a free
  // transcription slot".
  { stage: 'transcribing', re: /Download attempt|\[download\]|Downloading video|Video downloaded|Audio ready|Transcrib|Detected language|Reusing precomputed transcript|Job started/ },
];

const rank = (id) => STAGES.findIndex((s) => s.id === id);

export function deriveStage({ status, logs = [], queue = null } = {}) {
  const failed = status === 'failed';
  let stage = 'queued';
  if (status === 'completed') stage = 'done';
  else if (status !== 'queued') {
    for (const line of logs) {
      for (const m of MARKERS) {
        if (m.re.test(line) && rank(m.stage) > rank(stage)) { stage = m.stage; break; }
      }
    }
  }

  let detail = '';
  let pct = null;
  let clips = null;
  const tail = logs.length ? logs[logs.length - 1] : '';
  if (stage === 'queued') {
    if (queue) detail = queue.ahead === 0 ? 'Next in line' : `#${queue.position} in line`;
    else detail = 'Starting up';
  } else if (stage === 'transcribing') {
    for (let i = logs.length - 1; i >= 0; i--) {
      const t = /Transcribing… (\d+)%/.exec(logs[i]);
      if (t) { pct = Number(t[1]); break; }
    }
    const d = [...logs].reverse().find((l) => /\[download\]\s+(\d+)(?:\.\d+)?% of|Downloading video… (\d+)%/.test(l));
    detail = pct != null ? `Transcribing ${pct}%` : d ? 'Fetching the source' : 'Transcribing the speech';
  } else if (stage === 'scoring') {
    detail = 'Picking the strongest moments';
  } else if (stage === 'rendering') {
    let total = null;
    let ready = 0;
    for (const l of logs) {
      const f = /Found (\d+) (?:viral )?clips/.exec(l);
      if (f) total = Number(f[1]);
      if (/Clip \d+ ready|CLIP_READY/.test(l)) ready += 1;
    }
    clips = { ready, total };
    let fetched = null;
    for (const l of logs) {
      const m = /(\d+)\/(\d+) pieces downloaded/.exec(l);
      if (m) fetched = m;
      const r = /ranges: (\d+) pieces?/.exec(l);
      if (r && !fetched) fetched = [null, '0', r[1]];
    }
    detail = ready === 0 && fetched && fetched[1] !== fetched[2]
      ? `Fetching clip footage ${fetched[1]}/${fetched[2]}`
      : total ? `${ready} of ${total} clips ready` : 'Cutting and reframing';
  } else if (stage === 'done') {
    detail = 'All clips ready';
  }
  if (stage === 'transcribing' && /free transcription slot/.test(tail)) {
    stage = 'queued';
    detail = 'Waiting for a free transcription slot';
  }
  if (failed) detail = tail ? tail.replace(/^Error:\s*/, '').slice(0, 120) : 'Failed';
  return { stage, index: rank(stage), failed, detail, pct, clips };
}
