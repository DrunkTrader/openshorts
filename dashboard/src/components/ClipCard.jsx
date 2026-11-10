import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from 'react';
import { Play, Pause } from 'lucide-react';
import { apiJson } from '../lib/api';
import { loadPeaks } from '../lib/peaks';

const mq = (q) => (typeof window !== 'undefined' && window.matchMedia ? window.matchMedia(q) : null);

function useMedia(query) {
  const [on, setOn] = useState(() => !!mq(query)?.matches);
  useEffect(() => {
    const m = mq(query);
    if (!m) return undefined;
    const h = () => setOn(m.matches);
    m.addEventListener('change', h);
    return () => m.removeEventListener('change', h);
  }, [query]);
  return on;
}

const fmt = (t) => `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, '0')}`;

// Peaks canvas with scene boundaries. Colours come from the theme tokens and
// are re-read when data-theme flips.
function Strip({ peaks, scenes, duration, progress, onSeek }) {
  const ref = useRef(null);
  const [theme, setTheme] = useState(0);
  useEffect(() => {
    const o = new MutationObserver(() => setTheme((n) => n + 1));
    o.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    return () => o.disconnect();
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
    const ink = cs.getPropertyValue('--color-ink').trim();
    const muted = cs.getPropertyValue('--color-muted').trim();
    const accent = cs.getPropertyValue('--color-accent').trim();
    g.clearRect(0, 0, w, h);
    const n = peaks ? peaks.length : 0;
    const bw = n ? w / n : 0;
    for (let i = 0; i < n; i++) {
      const bh = Math.max(2, peaks[i] * (h - 6));
      g.fillStyle = i / n <= progress ? ink : muted;
      g.globalAlpha = i / n <= progress ? 1 : 0.45;
      g.fillRect(i * bw + 0.5, (h - bh) / 2, Math.max(1, bw - 1.5), bh);
    }
    if (!n) {
      g.globalAlpha = 0.3;
      g.fillStyle = muted;
      g.fillRect(0, h / 2 - 0.5, w, 1);
    }
    g.globalAlpha = 1;
    g.fillStyle = accent;
    if (duration) for (const s of scenes) {
      if (s.start > 0.05) g.fillRect(Math.round((s.start / duration) * w), 0, 1, h);
    }
  }, [peaks, scenes, duration, progress, theme]);
  const seek = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    onSeek(Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)));
  };
  return (
    <div
      role="slider"
      tabIndex={0}
      aria-label="Seek clip"
      aria-valuemin={0}
      aria-valuemax={Math.round(duration || 0)}
      aria-valuenow={Math.round(progress * (duration || 0))}
      aria-valuetext={`${fmt(progress * (duration || 0))} of ${fmt(duration || 0)}`}
      onClick={seek}
      onKeyDown={(e) => {
        if (e.key === 'ArrowLeft') onSeek(Math.max(0, progress - 2 / (duration || 1)));
        if (e.key === 'ArrowRight') onSeek(Math.min(1, progress + 2 / (duration || 1)));
      }}
      className="cursor-pointer rounded-input focus-visible:outline-offset-2"
    >
      <canvas ref={ref} className="block w-full h-9" data-testid="clip-waveform" />
    </div>
  );
}

// 9:16 preview: preload=metadata, hover-to-play muted on pointer devices,
// tap-to-play on touch, paused whenever it scrolls off-screen. Peaks and scene
// boundaries sit in a strip under the picture.
// `sceneSource` {jobId, index}: scene boundaries come from the clip's scenes
// endpoint (an editor-grade call), so they are asked for once, on first intent.
const ClipCard = forwardRef(function ClipCard(
  { src, sceneSource, fit = 'contain', boxClass = 'aspect-[9/16]', overlay, className = '', onPlay, onPause, onEnded, onError, onLoadedMetadata, ...rest },
  forwardedRef,
) {
  const videoRef = useRef(null);
  useImperativeHandle(forwardedRef, () => videoRef.current);
  const boxRef = useRef(null);
  const hoverStarted = useRef(false);
  const pinned = useRef(false);
  const canHover = useMedia('(hover: hover)');
  const reduced = useMedia('(prefers-reduced-motion: reduce)');
  const [wantPeaks, setWantPeaks] = useState(false);
  const [peaks, setPeaks] = useState(null);
  const [scenes, setScenes] = useState([]);
  const [duration, setDuration] = useState(0);
  const [progress, setProgress] = useState(0);
  const [playing, setPlaying] = useState(false);
  const scenesAsked = useRef(false);

  // Off-screen: pause. Peaks are decoded only on intent (hover, focus, tap),
  // so a page of cards never downloads every clip a second time.
  useEffect(() => {
    const box = boxRef.current;
    if (!box || typeof IntersectionObserver === 'undefined') return undefined;
    const io = new IntersectionObserver(([e]) => {
      if (!e.isIntersecting) videoRef.current?.pause();
    }, { threshold: 0.25 });
    io.observe(box);
    return () => io.disconnect();
  }, []);

  useEffect(() => {
    if (!wantPeaks || !src) return undefined;
    let alive = true;
    const ctl = new AbortController();
    loadPeaks(src, 96, ctl.signal).then((p) => { if (alive) setPeaks(p); });
    return () => { alive = false; ctl.abort(); };
  }, [wantPeaks, src]);

  const askScenes = useCallback(() => {
    setWantPeaks(true);
    if (!sceneSource || scenesAsked.current) return;
    scenesAsked.current = true;
    apiJson(`/api/clip/${sceneSource.jobId}/${sceneSource.index}/scenes`)
      .then((d) => setScenes(Array.isArray(d?.scenes) ? d.scenes : []))
      .catch(() => {});
  }, [sceneSource]);

  const dwell = useRef(null);
  useEffect(() => () => clearTimeout(dwell.current), []);
  const hoverIn = (e) => {
    if (e.pointerType === 'touch' || !canHover) return;
    // Peaks and scenes cost a download and an editor call: only after a
    // deliberate dwell, never for a pointer sweeping across the page.
    clearTimeout(dwell.current);
    dwell.current = setTimeout(askScenes, 600);
    const v = videoRef.current;
    if (reduced || !v || !v.paused) return;
    hoverStarted.current = true;
    v.muted = true;
    v.play().catch(() => {});
  };
  const hoverOut = (e) => {
    clearTimeout(dwell.current);
    if (e.pointerType === 'touch') return;
    const v = videoRef.current;
    if (v && hoverStarted.current && !pinned.current) v.pause();
    hoverStarted.current = false;
    pinned.current = false;
  };
  const toggle = () => {
    const v = videoRef.current;
    if (!v) return;
    askScenes();
    if (v.paused) v.play().catch(() => {}); else v.pause();
  };
  const seek = (f) => {
    const v = videoRef.current;
    if (v && duration) v.currentTime = f * duration;
  };

  return (
    <div className={`flex flex-col ${className}`}>
      <div
        ref={boxRef}
        onPointerEnter={hoverIn}
        onPointerLeave={hoverOut}
        onFocusCapture={askScenes}
        className={`relative bg-black flex-1 min-h-0 group/video ${boxClass}`}
      >
        <video
          ref={videoRef}
          src={src}
          controls={canHover}
          preload="metadata"
          playsInline
          data-clip-video
          className={`w-full h-full ${fit === 'cover' ? 'object-cover' : 'object-contain'}`}
          onClick={() => { pinned.current = true; }}
          onLoadedMetadata={(e) => { setDuration(e.target.duration || 0); onLoadedMetadata?.(e); }}
          onTimeUpdate={(e) => setProgress(e.target.duration ? e.target.currentTime / e.target.duration : 0)}
          onPlay={(e) => { setPlaying(true); onPlay?.(e); }}
          onPause={(e) => { setPlaying(false); onPause?.(e); }}
          onEnded={onEnded}
          onError={onError}
          {...rest}
        />
        {!canHover && (
          <button
            type="button"
            onClick={toggle}
            aria-label={playing ? 'Pause clip' : 'Play clip'}
            className="absolute inset-0 flex items-center justify-center"
          >
            <span className={`w-14 h-14 rounded-full bg-black/55 text-white flex items-center justify-center transition-opacity duration-200 ${playing ? 'opacity-0' : 'opacity-100'}`}>
              {playing ? <Pause size={22} /> : <Play size={22} className="ml-0.5" />}
            </span>
          </button>
        )}
        {overlay}
      </div>
      <div className="px-3 pt-2 pb-2.5 bg-paper2 border-t border-rule">
        <Strip peaks={peaks} scenes={scenes} duration={duration} progress={progress} onSeek={seek} />
        <div className="flex justify-between readout tabular mt-1">
          <span>{fmt(progress * duration)}</span>
          <span>{scenes.length > 1 ? `${scenes.length} scenes` : ''}</span>
          <span>{fmt(duration)}</span>
        </div>
      </div>
    </div>
  );
});

export default ClipCard;
