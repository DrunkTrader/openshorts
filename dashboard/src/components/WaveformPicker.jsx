import { useEffect, useRef, useState } from 'react';
import { loadPeaks } from '../lib/peaks';

// In/out picker over the same decoded peaks the clip cards use (lib/peaks).
// Two slider handles; the strip itself seeks. Peaks are best effort: a source
// too big or undecodable draws the baseline and the handles still work.

const stamp = (t) => {
  if (!Number.isFinite(t)) return '--:--.-';
  const m = Math.floor(t / 60);
  return `${String(m).padStart(2, '0')}:${(t - m * 60).toFixed(1).padStart(4, '0')}`;
};

const round3 = (t) => Math.round(t * 1000) / 1000;

function Bars({ peaks, from, to, lo, hi }) {
  const ref = useRef(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const redraw = () => setTick((n) => n + 1);
    const o = new MutationObserver(redraw);
    o.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(redraw) : null;
    if (ro && ref.current) ro.observe(ref.current);
    return () => { o.disconnect(); ro?.disconnect(); };
  }, []);
  useEffect(() => {
    const c = ref.current;
    if (!c) return;
    const dpr = window.devicePixelRatio || 1;
    const w = c.clientWidth;
    const h = c.clientHeight;
    c.width = Math.round(w * dpr);
    c.height = Math.round(h * dpr);
    const g = c.getContext('2d');
    g.scale(dpr, dpr);
    const cs = getComputedStyle(document.documentElement);
    g.clearRect(0, 0, w, h);
    g.fillStyle = cs.getPropertyValue('--color-muted').trim();
    const n = peaks ? peaks.length : 0;
    if (!n) { g.globalAlpha = 0.4; g.fillRect(0, h / 2 - 0.5, w, 1); return; }
    // peaks cover [from, to] of the source; the strip covers [lo, hi]
    const span = hi - lo || 1;
    const x0 = ((from - lo) / span) * w;
    const bw = (((to - from) / span) * w) / n;
    g.globalAlpha = 0.7;
    for (let i = 0; i < n; i++) {
      const bh = Math.max(2, peaks[i] * (h - 8));
      g.fillRect(x0 + i * bw, (h - bh) / 2, Math.max(1, bw - 1), bh);
    }
  }, [peaks, from, to, lo, hi, tick]);
  return <canvas ref={ref} className="absolute inset-0 w-full h-full" data-testid="editor-waveform" aria-hidden="true" />;
}

function Handle({ label, value, set, lo, hi, min, max, onChange }) {
  const dragging = useRef(false);
  const onKeyDown = (e) => {
    const step = e.shiftKey ? 1 : 0.1;
    const to = { ArrowLeft: value - step, ArrowDown: value - step, ArrowRight: value + step, ArrowUp: value + step, Home: min, End: max }[e.key];
    if (to === undefined) return;
    e.preventDefault();
    e.stopPropagation();
    onChange(Math.min(max, Math.max(min, round3(to))));
  };
  const pct = ((value - lo) / (hi - lo || 1)) * 100;
  return (
    <div
      role="slider"
      tabIndex={0}
      aria-label={label}
      aria-valuemin={round3(min)}
      aria-valuemax={round3(max)}
      aria-valuenow={round3(value)}
      aria-valuetext={stamp(value)}
      data-set={set ? '1' : '0'}
      onKeyDown={onKeyDown}
      onPointerDown={(e) => {
        if (e.button !== undefined && e.button !== 0) return;
        e.preventDefault();
        e.stopPropagation();
        e.currentTarget.focus();
        e.currentTarget.setPointerCapture?.(e.pointerId);
        dragging.current = true;
      }}
      onPointerMove={(e) => {
        if (!dragging.current) return;
        const box = e.currentTarget.parentElement.getBoundingClientRect();
        const t = lo + ((e.clientX - box.left) / box.width) * (hi - lo);
        onChange(Math.min(max, Math.max(min, round3(t))));
      }}
      onPointerUp={() => { dragging.current = false; }}
      onPointerCancel={() => { dragging.current = false; }}
      className="absolute inset-y-0 -ml-[22px] w-11 sm:-ml-3 sm:w-6 flex justify-center touch-none cursor-ew-resize outline-none group"
      style={{ left: `${pct}%` }}
    >
      <span className={`w-1 h-full rounded-full group-focus-visible:ring-2 group-focus-visible:ring-offset-1 group-focus-visible:ring-[color:var(--color-focus)] ${set ? 'bg-brass' : 'bg-ink/50'}`} />
    </div>
  );
}

/**
 * src: media URL for peaks (may be null). lo/hi: the source domain in seconds.
 * span: [from, to] of the source the decoded file covers (a padded piece).
 * inT/outT: effective handle values; inSet/outSet: whether the user marked them.
 */
export default function WaveformPicker({ src, lo, hi, span, inT, outT, inSet, outSet, playhead, minGap, onIn, onOut, onSeek }) {
  const [peaks, setPeaks] = useState(null);
  useEffect(() => {
    if (!src) return undefined;
    let alive = true;
    const ctl = new AbortController();
    loadPeaks(src, 120, ctl.signal).then((p) => { if (alive) setPeaks(p); });
    return () => { alive = false; ctl.abort(); };
  }, [src]);
  const from = span?.[0] ?? lo;
  const to = span?.[1] ?? hi;
  const pct = (t) => `${Math.min(100, Math.max(0, ((t - lo) / (hi - lo || 1)) * 100))}%`;
  const a = Math.min(inT, outT);
  const b = Math.max(inT, outT);
  return (
    <div className="shrink-0 select-none">
      <div className="flex items-center justify-between mb-1.5 gap-3">
        <p className="readout">WAVEFORM · IN {stamp(inT)} · OUT {stamp(outT)}</p>
        <p className="readout hidden xl:block truncate">[ ] SET · ARROWS NUDGE 0.1 S · SHIFT 1 S</p>
      </div>
      <div
        className="relative h-16 rounded-input border border-rule bg-paper overflow-hidden"
        onPointerDown={(e) => {
          const box = e.currentTarget.getBoundingClientRect();
          onSeek(lo + ((e.clientX - box.left) / box.width) * (hi - lo));
        }}
      >
        <Bars peaks={peaks} from={from} to={to} lo={lo} hi={hi} />
        <div className="absolute inset-y-0 bg-brass/15 pointer-events-none" style={{ left: pct(a), width: `calc(${pct(b)} - ${pct(a)})` }} />
        <div className="absolute inset-y-0 w-px bg-ink pointer-events-none" style={{ left: pct(playhead) }} />
        <Handle label="in point" value={inT} set={inSet} lo={lo} hi={hi} min={lo} max={Math.max(lo, outSet ? outT - minGap : hi)} onChange={onIn} />
        <Handle label="out point" value={outT} set={outSet} lo={lo} hi={hi} min={Math.min(hi, inSet ? inT + minGap : lo)} max={hi} onChange={onOut} />
      </div>
    </div>
  );
}
