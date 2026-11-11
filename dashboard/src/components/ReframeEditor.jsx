import React, { useState, useEffect, useCallback, useRef } from 'react';
import { X, Loader2, Crosshair, RotateCcw, AlertCircle, Play, Pause, Columns2, Trash2, Undo2, Redo2, Keyboard } from 'lucide-react';
import { getApiUrl } from '../config';
import { apiJson, apiFetch } from '../lib/api';
import { useShortcuts, useDialog } from '../lib/shortcuts';
import ShortcutHelp from './ShortcutHelp';
import Button from './ui/Button';
import SegmentedControl from './ui/SegmentedControl';

// Manual reframing: the automatic crop is right most of the time and grossly
// wrong occasionally, and until now there was no way to say "frame it here".
//
// The unit is the scene, because a podcast cuts between a fixed close camera
// and a fixed wide one and the right crop differs per camera. Scenes the user
// does not touch stay automatic, so fixing one bad shot cannot spoil the good
// ones — only adjusted scenes are sent.
//
// Two things a still frame cannot tell you, and both are handled here:
//   - WHO is talking. Each scene plays its own range of an uncropped preview,
//     with sound, and the rectangle stays overlaid while it plays.
//   - Whether one window is even enough. A scene can be split into two stacked
//     regions, positioned independently.

const fmt = (s) => {
    const m = Math.floor(s / 60);
    const r = Math.floor(s % 60);
    return `${m}:${String(r).padStart(2, '0')}`;
};

export default function ReframeEditor({ jobId, clipIndex, clipTitle, onClose, onReframed }) {
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);
    const [data, setData] = useState(null);
    const [overrides, setOverrides] = useState({});   // idx -> number | {top,bottom}
    const [playing, setPlaying] = useState(null);     // scene index being played
    const [saving, setSaving] = useState(false);
    const [helpOpen, setHelpOpen] = useState(false);
    const [activeScene, setActiveScene] = useState(null);
    const [activeHalf, setActiveHalf] = useState('top');
    const boxRef = useRef(null);
    const helpBtnRef = useRef(null);
    useDialog(boxRef, !helpOpen, !loading);

    // Undo history of the crop edits. Edits of one gesture (a drag, a run of
    // arrow presses) share a tag and land as ONE step.
    const overridesRef = useRef({});
    const hist = useRef({ past: [], future: [], tag: null, at: 0 });
    const [histSize, setHistSize] = useState({ past: 0, future: 0 });
    const syncHist = () => setHistSize({ past: hist.current.past.length, future: hist.current.future.length });
    const commit = useCallback((tag, fn) => {
        const h = hist.current;
        const next = fn(overridesRef.current);
        const now = Date.now();
        if (!(tag && h.tag === tag && now - h.at < 600)) {
            h.past.push(overridesRef.current);
            if (h.past.length > 100) h.past.shift();
        }
        h.tag = tag;
        h.at = now;
        h.future = [];
        overridesRef.current = next;
        setOverrides(next);
        syncHist();
    }, []);
    const travel = useCallback((from, to) => {
        const h = hist.current;
        if (!h[from].length) return;
        h[to].push(overridesRef.current);
        overridesRef.current = h[from].pop();
        h.tag = null;
        setOverrides(overridesRef.current);
        syncHist();
    }, []);
    const undo = useCallback(() => travel('past', 'future'), [travel]);
    const redo = useCallback(() => travel('future', 'past'), [travel]);

    useEffect(() => {
        let alive = true;
        (async () => {
            try {
                const res = await apiJson(`/api/clip/${jobId}/${clipIndex}/scenes`);
                if (!alive) return;
                setData(res);
                // Start from what is already applied. A re-render rebuilds
                // from source with only what it receives, so opening blank
                // would quietly discard every earlier adjustment on the next
                // save.
                const salvos = res.saved_overrides || {};
                overridesRef.current = Object.fromEntries(
                    Object.entries(salvos).map(([k, v]) => [Number(k), v])
                );
                setOverrides(overridesRef.current);
            } catch (e) {
                if (alive) setError(e?.message || 'Could not read the scenes of this clip.');
            } finally {
                if (alive) setLoading(false);
            }
        })();
        return () => { alive = false; };
    }, [jobId, clipIndex]);

    const half = (data?.crop_width_fraction ?? 0.5) / 2;
    const clamp = useCallback((v) => Math.min(1 - half, Math.max(half, v)), [half]);

    // What a scene shows right now: the user's value, else the backend's
    // suggestion (the biggest face in the shot).
    const valueOf = useCallback((scene) => (
        overrides[scene.index] ?? clamp(scene.suggested_center)
    ), [overrides, clamp]);

    const setSingle = useCallback((idx, fraction) => {
        commit(`s${idx}`, (o) => ({ ...o, [idx]: clamp(fraction) }));
    }, [clamp, commit]);

    const setSplitHalf = useCallback((idx, which, fraction) => {
        commit(`h${idx}${which}`, (o) => {
            const cur = o[idx];
            const base = (cur && typeof cur === 'object')
                ? cur
                : { top: { x: clamp(0.3), y: 0.5 }, bottom: { x: clamp(0.7), y: 0.5 } };
            const anterior = base[which] || { y: 0.5 };
            return { ...o, [idx]: {
                ...base,
                [which]: { x: clamp(fraction), y: anterior.y ?? 0.5 },
            } };
        });
    }, [clamp, commit]);

    const toggleSplit = useCallback((idx, scene) => {
        commit(null, (o) => {
            const cur = o[idx];
            if (cur && typeof cur === 'object') {
                // back to a single window, kept where the top half was
                return { ...o, [idx]: cur.top?.x ?? 0.5 };
            }
            const centre = typeof cur === 'number' ? cur : clamp(scene.suggested_center);
            // SPLIT halves crop vertically as well, so each carries the face
            // height the backend measured for this scene.
            const y = scene.suggested_center_y ?? 0.5;
            return { ...o, [idx]: {
                top: { x: clamp(centre - 0.2), y },
                bottom: { x: clamp(centre + 0.2), y },
            } };
        });
    }, [clamp, commit]);

    const resetScene = useCallback((idx) => {
        commit(null, (o) => {
            const next = { ...o };
            delete next[idx];
            return next;
        });
    }, [commit]);

    const adjusted = Object.keys(overrides).length;
    const scenes = data?.scenes;
    // The scene the keyboard acts on: the one last touched, else the first.
    const sceneNow = scenes?.find((sc) => sc.index === activeScene) || scenes?.[0];

    // Arrow keys nudge the crop of the active scene: 1%, Shift 5%.
    const nudge = (dir, big) => {
        if (!sceneNow) return false;
        const v = valueOf(sceneNow);
        const d = dir * (big ? 0.05 : 0.01);
        if (v && typeof v === 'object') setSplitHalf(sceneNow.index, activeHalf, v[activeHalf].x + d);
        else setSingle(sceneNow.index, v + d);
        return true;
    };

    const handleSave = async () => {
        if (!adjusted || saving) return;
        setSaving(true);
        setError(null);
        try {
            const payload = Object.fromEntries(Object.entries(overrides).map(([k, v]) => [
                String(k),
                typeof v === 'object'
                    ? { top: { x: Number(v.top.x.toFixed(4)), y: Number(v.top.y.toFixed(4)) },
                        bottom: { x: Number(v.bottom.x.toFixed(4)), y: Number(v.bottom.y.toFixed(4)) } }
                    : Number(v.toFixed(4)),
            ]));
            const res = await apiJson('/api/clip/reframe', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ job_id: jobId, clip_index: clipIndex, crop_overrides: payload }),
            });
            if (onReframed) onReframed(clipIndex, res);
            onClose();
        } catch (e) {
            setError(e?.message || 'The re-render failed.');
        } finally {
            setSaving(false);
        }
    };

    useShortcuts({
        escape: () => { onClose(); },
        '?': () => setHelpOpen(true),
        space: () => { if (!sceneNow) return false; setPlaying((p) => (p === sceneNow.index ? null : sceneNow.index)); return true; },
        s: () => { if (!sceneNow) return false; toggleSplit(sceneNow.index, sceneNow); return true; },
        arrowleft: () => nudge(-1, false),
        arrowright: () => nudge(1, false),
        'shift+arrowleft': () => nudge(-1, true),
        'shift+arrowright': () => nudge(1, true),
        z: undo,
        'shift+z': redo,
        'mod+z': undo,
        'mod+shift+z': redo,
        'mod+enter': () => { handleSave(); },
    }, !helpOpen);

    return (
        /* Bottom sheet on a phone, centred dialog from sm — same shape as the
           shared Modal so the app has one overlay idiom, not two. */
        <div className="fixed inset-0 z-50 bg-black/80 flex items-end sm:items-center justify-center p-0 sm:p-4">
            <div
                ref={boxRef}
                role="dialog"
                aria-modal="true"
                aria-label={`reframe clip ${clipIndex + 1}`}
                tabIndex={-1}
                className="outline-none card w-full max-w-3xl max-h-[92vh] sm:max-h-[90vh] flex flex-col rounded-b-none sm:rounded-card animate-sheet-up sm:animate-none"
            >
                <div className="flex items-center justify-between p-4 border-b border-rule">
                    <div className="flex items-center gap-2.5 min-w-0">
                        <Crosshair size={18} className="text-brass shrink-0" />
                        <div className="min-w-0">
                            <h2 className="text-base font-medium text-ink lowercase truncate">reframing</h2>
                            {clipTitle && <p className="text-xs text-muted truncate">{clipTitle}</p>}
                        </div>
                    </div>
                    <div className="flex items-center gap-1 shrink-0">
                        <button onClick={undo} disabled={!histSize.past} aria-label="undo crop edit"
                                className="p-2 rounded-input text-muted hover:text-ink hover:bg-paper3 disabled:opacity-45 min-h-6 min-w-6">
                            <Undo2 size={16} />
                        </button>
                        <button onClick={redo} disabled={!histSize.future} aria-label="redo crop edit"
                                className="p-2 rounded-input text-muted hover:text-ink hover:bg-paper3 disabled:opacity-45 min-h-6 min-w-6">
                            <Redo2 size={16} />
                        </button>
                        <button ref={helpBtnRef} onClick={() => setHelpOpen(true)} aria-label="keyboard shortcuts"
                                className="p-2 rounded-input text-muted hover:text-ink hover:bg-paper3 min-h-6 min-w-6">
                            <Keyboard size={16} />
                        </button>
                        <button onClick={onClose} aria-label="close reframing"
                                className="p-2 rounded-input text-muted hover:text-ink hover:bg-paper3 transition-colors min-h-6 min-w-6">
                            <X size={18} />
                        </button>
                    </div>
                </div>

                <div className="flex-1 overflow-y-auto overscroll-contain custom-scrollbar p-4 space-y-5">
                    <SlotSplitPanel
                        jobId={jobId}
                        clipIndex={clipIndex}
                        onDone={(res) => {
                            if (onReframed) onReframed(clipIndex, res);
                            onClose();
                        }}
                    />

                    <p className="text-xs text-muted leading-relaxed">
                        Play a scene to hear who is talking, then drag the rectangle over
                        the person you want. Each scene is one camera. Scenes you leave
                        alone keep the automatic camera.
                    </p>

                    {loading && (
                        <div className="flex items-center gap-2 text-sm text-muted py-8 justify-center">
                            <Loader2 size={18} className="animate-spin text-brass" />
                            reading the scenes…
                        </div>
                    )}

                    {error && (
                        <div className="flex items-start gap-2 text-xs text-warn border border-warn/40 rounded-input p-3">
                            <AlertCircle size={14} className="shrink-0 mt-0.5" />
                            <span>{error}</span>
                        </div>
                    )}

                    {data?.scenes?.map((scene) => (
                        <SceneRow
                            key={scene.index}
                            scene={scene}
                            value={valueOf(scene)}
                            widthFraction={data.crop_width_fraction}
                            previewUrl={data.preview_url}
                            touched={scene.index in overrides}
                            playing={playing === scene.index}
                            onPlayToggle={() => setPlaying((p) => (p === scene.index ? null : scene.index))}
                            onMoveSingle={(f) => setSingle(scene.index, f)}
                            onMoveHalf={(which, f) => setSplitHalf(scene.index, which, f)}
                            onToggleSplit={() => toggleSplit(scene.index, scene)}
                            onReset={() => resetScene(scene.index)}
                            onActive={(half) => { setActiveScene(scene.index); if (half) setActiveHalf(half); }}
                        />
                    ))}
                </div>

                <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 p-4 border-t border-rule">
                    <span className="text-xs text-muted">
                        {adjusted === 0
                            ? 'nothing adjusted yet'
                            : `${adjusted} scene${adjusted > 1 ? 's' : ''} reframed by hand`}
                    </span>
                    <div className="flex items-center gap-2 [&>button]:flex-1 sm:[&>button]:flex-none">
                        <Button variant="quiet" onClick={onClose} className="min-h-6">cancel</Button>
                        <Button onClick={handleSave} disabled={!adjusted || saving} className="min-h-6">
                            {saving ? <Loader2 size={16} className="animate-spin" /> : null}
                            {saving ? 're-rendering…' : 'apply reframing'}
                        </Button>
                    </div>
                </div>
                <div className="sm:hidden safe-bottom" />
            </div>
            <ShortcutHelp isOpen={helpOpen} onClose={() => setHelpOpen(false)} scope="reframe" returnFocusRef={helpBtnRef} />
        </div>
    );
}


// One scene. While it plays, the frame is replaced by the uncropped preview
// seeked to this scene, so the rectangle can be judged against moving pictures
// and sound rather than a single still.
function SceneRow({ scene, value, widthFraction, previewUrl, touched, playing,
                    onPlayToggle, onMoveSingle, onMoveHalf, onToggleSplit, onReset, onActive }) {
    const boxRef = useRef(null);
    const videoRef = useRef(null);
    const [dragging, setDragging] = useState(null);   // null | 'single' | 'top' | 'bottom'

    const isSplit = value && typeof value === 'object';

    // Play only this scene's slice of the shared preview.
    useEffect(() => {
        const v = videoRef.current;
        if (!v) return;
        if (!playing) { v.pause(); return; }
        v.currentTime = scene.start;
        v.play().catch(() => {});
        const stopAtEnd = () => { if (v.currentTime >= scene.end) { v.pause(); } };
        v.addEventListener('timeupdate', stopAtEnd);
        return () => v.removeEventListener('timeupdate', stopAtEnd);
    }, [playing, scene.start, scene.end]);

    const fractionFromEvent = useCallback((clientX) => {
        const el = boxRef.current;
        if (!el) return 0.5;
        const rect = el.getBoundingClientRect();
        return (clientX - rect.left) / rect.width;
    }, []);

    useEffect(() => {
        if (!dragging) return;
        const move = (e) => {
            const x = e.touches ? e.touches[0].clientX : e.clientX;
            const f = fractionFromEvent(x);
            if (dragging === 'single') onMoveSingle(f);
            else onMoveHalf(dragging, f);
        };
        const up = () => setDragging(null);
        window.addEventListener('mousemove', move);
        window.addEventListener('mouseup', up);
        window.addEventListener('touchmove', move);
        window.addEventListener('touchend', up);
        return () => {
            window.removeEventListener('mousemove', move);
            window.removeEventListener('mouseup', up);
            window.removeEventListener('touchmove', move);
            window.removeEventListener('touchend', up);
        };
    }, [dragging, fractionFromEvent, onMoveSingle, onMoveHalf]);

    const startDrag = (which) => (e) => {
        e.stopPropagation();
        setDragging(which);
        const x = e.touches ? e.touches[0].clientX : e.clientX;
        if (which === 'single') onMoveSingle(fractionFromEvent(x));
        else onMoveHalf(which, fractionFromEvent(x));
    };

    const nudgeWin = (which, centre) => (e) => {
        const dir = { ArrowLeft: -1, ArrowRight: 1 }[e.key];
        if (!dir) return;
        e.preventDefault();
        e.stopPropagation();
        const f = centre + dir * (e.shiftKey ? 0.05 : 0.01);
        if (which === 'single') onMoveSingle(f); else onMoveHalf(which, f);
    };

    const win = (centre, label, which) => {
        const leftPct = (centre - widthFraction / 2) * 100;
        return (
            <div
                key={which}
                role="slider"
                tabIndex={0}
                aria-label={`crop position, scene ${scene.index + 1}${label ? `, ${label} region` : ''}`}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={Math.round(centre * 100)}
                aria-valuetext={`${Math.round(centre * 100)}% across`}
                onFocus={() => onActive(which === 'single' ? null : which)}
                onKeyDown={nudgeWin(which, centre)}
                onMouseDown={startDrag(which)}
                onTouchStart={startDrag(which)}
                className="absolute inset-y-0 border-2 border-brass cursor-ew-resize focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[color:var(--color-focus)]"
                style={{ left: `${leftPct}%`, width: `${widthFraction * 100}%`, minWidth: 24 }}
            >
                {label && (
                    <span className="absolute top-1 left-1 text-[10px] px-1 rounded bg-brass text-paper lowercase">
                        {label}
                    </span>
                )}
            </div>
        );
    };

    return (
        <div className="space-y-1.5" onPointerDownCapture={() => onActive(null)}>
            <div className="flex items-center justify-between text-xs gap-2">
                <div className="flex items-center gap-2 min-w-0">
                    <button
                        onClick={onPlayToggle}
                        className="flex items-center justify-center gap-1 min-h-6 min-w-6 text-ink2 hover:text-brass transition-colors"
                        title="play this scene with sound"
                        aria-label={playing ? `pause scene ${scene.index + 1}` : `play scene ${scene.index + 1} with sound`}
                    >
                        {playing ? <Pause size={13} /> : <Play size={13} />}
                    </button>
                    <span className="readout text-muted truncate">
                        scene {scene.index + 1} · {fmt(scene.start)}–{fmt(scene.end)}
                    </span>
                </div>
                <div className="flex items-center gap-3 shrink-0">
                    <button
                        onClick={onToggleSplit}
                        aria-pressed={isSplit}
                        className={`flex items-center gap-1 min-h-6 min-w-6 transition-colors ${
                            isSplit ? 'text-brass' : 'text-muted hover:text-ink2'}`}
                        title="stack two regions instead of one window"
                    >
                        <Columns2 size={12} /> split
                    </button>
                    {touched ? (
                        <button onClick={onReset} className="flex items-center gap-1 min-h-6 text-brass hover:underline">
                            <RotateCcw size={12} /> automatic
                        </button>
                    ) : (
                        <span className="text-muted">automatic</span>
                    )}
                </div>
            </div>

            <div
                ref={boxRef}
                className={`relative overflow-hidden rounded-input select-none border ${
                    touched ? 'border-brass' : 'border-rule'}`}
            >
                {playing && previewUrl ? (
                    <video
                        ref={videoRef}
                        src={getApiUrl(previewUrl)}
                        playsInline
                        className="w-full block"
                    />
                ) : scene.thumbnail_url ? (
                    <img
                        src={getApiUrl(scene.thumbnail_url)}
                        alt=""
                        draggable={false}
                        className="w-full block pointer-events-none"
                    />
                ) : (
                    <div className="w-full aspect-video bg-paper3" />
                )}

                {/* Everything outside the kept region is dimmed, so what survives
                    the crop is what stays bright. */}
                {!isSplit && (
                    <>
                        <div className="absolute inset-y-0 left-0 bg-black/65 pointer-events-none"
                             style={{ width: `${Math.max(0, (value - widthFraction / 2) * 100)}%` }} />
                        <div className="absolute inset-y-0 right-0 bg-black/65 pointer-events-none"
                             style={{ width: `${Math.max(0, 100 - (value + widthFraction / 2) * 100)}%` }} />
                    </>
                )}

                {isSplit
                    ? [win(value.top.x, 'top', 'top'), win(value.bottom.x, 'bottom', 'bottom')]
                    : win(value, null, 'single')}
            </div>

            {isSplit && (
                <p className="text-[11px] text-muted leading-snug">
                    Two regions stacked in the vertical frame: <strong>top</strong> above,
                    <strong> bottom</strong> below. Drag each one onto the person it should hold.
                </p>
            )}
        </div>
    );
}


const STEP = 0.02;
const clamp01 = (v) => Math.min(1, Math.max(0, v));

// Clip-level "split with clip": each half of the 9:16 frame is the clip itself
// or an uploaded video. Separate from the per-scene "split" above, which
// stacks two regions of the same source.
function SlotSplitPanel({ jobId, clipIndex, onDone }) {
    const [open, setOpen] = useState(false);
    const [library, setLibrary] = useState([]);
    const [libError, setLibError] = useState(null);
    const [uploading, setUploading] = useState(false);
    const [top, setTop] = useState({ kind: 'source', upload_id: '', offset_s: 0, fit: { x: 0.5, y: 0.5 } });
    const [bottom, setBottom] = useState({ kind: 'upload', upload_id: '', offset_s: 0, fit: { x: 0.5, y: 0.5 } });
    const [ratio, setRatio] = useState('50/50');
    const [audio, setAudio] = useState('source');
    const [loop, setLoop] = useState(true);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState(null);

    const loadLibrary = useCallback(async () => {
        try {
            const res = await apiJson('/api/slot-uploads');
            setLibrary(res.items || []);
            setLibError(null);
        } catch (e) {
            setLibError(e?.detail || 'Could not load your clip library.');
        }
    }, []);

    useEffect(() => { if (open) loadLibrary(); }, [open, loadLibrary]);

    const upload = async (file) => {
        if (!file) return;
        setUploading(true);
        setLibError(null);
        try {
            const body = new FormData();
            body.append('file', file);
            const res = await apiFetch('/api/slot-uploads', { method: 'POST', body });
            if (!res.ok) {
                let detail = '';
                try { detail = (await res.json()).detail; } catch (_) { /* not JSON */ }
                throw new Error(typeof detail === 'string' && detail ? detail : 'Upload failed.');
            }
            const item = await res.json();
            await loadLibrary();
            // The new clip goes where the user is most likely to want it.
            setBottom((b) => (b.kind === 'upload' && !b.upload_id ? { ...b, upload_id: item.id } : b));
        } catch (e) {
            setLibError(e?.message || 'Upload failed.');
        } finally {
            setUploading(false);
        }
    };

    const remove = async (id) => {
        try {
            await apiJson(`/api/slot-uploads/${id}`, { method: 'DELETE' });
            setTop((t) => (t.upload_id === id ? { ...t, upload_id: '' } : t));
            setBottom((b) => (b.upload_id === id ? { ...b, upload_id: '' } : b));
            await loadLibrary();
        } catch (e) {
            setLibError(e?.detail || 'Could not delete this clip.');
        }
    };

    const slotBody = (s) => (s.kind === 'source'
        ? { kind: 'source' }
        : { kind: 'upload', upload_id: s.upload_id, offset_s: Number(s.offset_s) || 0,
            fit: { x: Number(s.fit.x.toFixed(4)), y: Number(s.fit.y.toFixed(4)) } });

    const missing = [top, bottom].some((s) => s.kind === 'upload' && !s.upload_id);

    const apply = async () => {
        if (busy || missing) return;
        setBusy(true);
        setError(null);
        try {
            const res = await apiJson('/api/clip/split', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    job_id: jobId, clip_index: clipIndex,
                    slots: { top: slotBody(top), bottom: slotBody(bottom), ratio, audio, loop },
                }),
            });
            onDone(res);
        } catch (e) {
            setError(e?.detail || e?.message || 'The split render failed.');
        } finally {
            setBusy(false);
        }
    };

    return (
        <section className="rounded-card border border-rule bg-paper2" aria-labelledby="slot-split-title">
            <h3 id="slot-split-title" className="font-display text-lg text-ink lowercase">
                <button
                    type="button"
                    onClick={() => setOpen((o) => !o)}
                    aria-expanded={open}
                    aria-controls="slot-split-body"
                    className="w-full flex items-center gap-2 min-h-6 px-4 py-3 text-left rounded-card hover:bg-paper3 transition-colors"
                >
                    <Columns2 size={16} className="text-brass" /> split with clip
                    <span className="readout ml-auto">{open ? 'OPEN' : 'GAMEPLAY, B-ROLL'}</span>
                </button>
            </h3>
            {open && (
                <div id="slot-split-body" className="px-4 pb-4 pt-1 space-y-5 animate-fade">
                    <p className="text-xs text-muted leading-relaxed">
                        Stack this clip with a video of your own, such as gameplay. Pick what
                        each half shows, then drag the picture (or use the arrow keys) to
                        choose which part stays in frame.
                    </p>

                    <div className="grid sm:grid-cols-2 gap-4">
                        <SlotControl id="top" label="top half" slot={top} onChange={setTop} library={library} />
                        <SlotControl id="bottom" label="bottom half" slot={bottom} onChange={setBottom} library={library} />
                    </div>

                    <fieldset className="flex flex-wrap items-end gap-x-6 gap-y-3 text-xs">
                        <legend className="sr-only">split settings</legend>
                        <div>
                            <p className="eyebrow mb-1.5" id="split-ratio-label">Ratio</p>
                            <div role="group" aria-labelledby="split-ratio-label" className="w-40">
                                <SegmentedControl
                                    size="sm"
                                    columns={2}
                                    value={ratio}
                                    onChange={setRatio}
                                    options={[{ value: '50/50', label: '50/50' }, { value: '60/40', label: '60/40' }]}
                                />
                            </div>
                        </div>
                        <label className="block">
                            <span className="eyebrow block mb-1.5">Audio</span>
                            <select value={audio} onChange={(e) => setAudio(e.target.value)}
                                    className="input-field py-1.5 min-h-6">
                                <option value="source">this clip</option>
                                <option value="upload">uploaded video</option>
                                <option value="mix">mix of both</option>
                            </select>
                        </label>
                        <label className="flex items-center gap-2 min-h-6 text-ink2 cursor-pointer">
                            <input type="checkbox" checked={loop} onChange={(e) => setLoop(e.target.checked)}
                                   className="w-4 h-4 accent-[color:var(--color-accent)]" />
                            <span>loop uploaded video</span>
                        </label>
                    </fieldset>

                    <div className="space-y-2">
                        <p className="eyebrow">Your videos</p>
                        <label
                            htmlFor="slot-upload-input"
                            className="flex items-center justify-center gap-2 min-h-11 px-4 py-3 rounded-input border border-dashed border-rule2 text-xs text-ink2 cursor-pointer hover:bg-paper3 transition-colors focus-within:outline focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-[color:var(--color-focus)]"
                        >
                            add a video (MP4, MOV, WebM or MKV)
                            <input id="slot-upload-input" type="file" accept=".mp4,.mov,.webm,.mkv,video/*"
                                   disabled={uploading}
                                   onChange={(e) => { upload(e.target.files?.[0]); e.target.value = ''; }}
                                   className="sr-only" />
                        </label>
                        {uploading && (
                            <p role="status" className="text-xs text-muted flex items-center gap-1.5">
                                <Loader2 size={12} className="animate-spin" /> uploading…
                            </p>
                        )}
                        {libError && <p role="alert" className="text-xs text-warn">{libError}</p>}
                        {library.length === 0 && !libError && (
                            <p className="text-xs text-muted">Your library is empty. Add a clip to put it in a half.</p>
                        )}
                        <ul className="divide-y divide-[color:var(--color-rule)]">
                            {library.map((item) => (
                                <li key={item.id} className="flex items-center gap-2 py-1.5 text-xs">
                                    {item.thumb && (
                                        <img src={getApiUrl(item.thumb)} alt="" className="w-10 h-7 object-cover rounded-sm" />
                                    )}
                                    <span className="truncate flex-1 text-ink2">{item.name}</span>
                                    <span className="readout">{fmt(item.duration || 0)}</span>
                                    <button type="button" onClick={() => remove(item.id)}
                                            aria-label={`delete ${item.name}`}
                                            className="p-1.5 min-h-6 min-w-6 flex items-center justify-center rounded-input text-muted hover:text-danger hover:bg-paper3">
                                        <Trash2 size={13} />
                                    </button>
                                </li>
                            ))}
                        </ul>
                    </div>

                    {error && (
                        <p role="alert" className="flex items-start gap-2 text-xs text-danger">
                            <AlertCircle size={14} className="shrink-0 mt-0.5" /> {error}
                        </p>
                    )}
                    <div className="flex flex-wrap items-center gap-3">
                        <Button onClick={apply} disabled={busy || missing} className="min-h-6">
                            {busy ? <Loader2 size={16} className="animate-spin" /> : null}
                            {busy ? 'rendering the split…' : 'apply split'}
                        </Button>
                        {missing && <span className="text-xs text-muted">choose an uploaded video for each upload half</span>}
                    </div>
                </div>
            )}
        </section>
    );
}

// One half: source or upload, which upload, where it starts, and where the
// cover crop sits. The crop preview is a focusable rectangle: drag it with a
// pointer or move it with the arrow keys.
function SlotControl({ id, label, slot, onChange, library }) {
    const set = (patch) => onChange({ ...slot, ...patch });
    const picked = library.find((i) => i.id === slot.upload_id);
    const areaRef = useRef(null);

    const moveTo = (clientX, clientY) => {
        const el = areaRef.current;
        if (!el) return;
        const r = el.getBoundingClientRect();
        set({ fit: { x: clamp01((clientX - r.left) / r.width), y: clamp01((clientY - r.top) / r.height) } });
    };

    const onPointerDown = (e) => {
        e.currentTarget.setPointerCapture?.(e.pointerId);
        moveTo(e.clientX, e.clientY);
    };
    const onPointerMove = (e) => {
        if (e.currentTarget.hasPointerCapture?.(e.pointerId)) moveTo(e.clientX, e.clientY);
    };
    const onKeyDown = (e) => {
        const d = { ArrowLeft: [-STEP, 0], ArrowRight: [STEP, 0], ArrowUp: [0, -STEP], ArrowDown: [0, STEP] }[e.key];
        if (!d) return;
        e.preventDefault();
        set({ fit: { x: clamp01(slot.fit.x + d[0]), y: clamp01(slot.fit.y + d[1]) } });
    };

    return (
        <div className="space-y-2.5 text-xs">
            <label className="block">
                <span className="eyebrow">{label}</span>
                <select value={slot.kind} onChange={(e) => set({ kind: e.target.value })}
                        className="input-field mt-1.5 py-1.5 min-h-6">
                    <option value="source">this clip</option>
                    <option value="upload">uploaded video</option>
                </select>
            </label>
            {slot.kind === 'upload' && (
                <>
                    <label className="block">
                        <span className="text-muted">video for the {label}</span>
                        <select value={slot.upload_id} onChange={(e) => set({ upload_id: e.target.value })}
                                className="input-field mt-1.5 py-1.5 min-h-6">
                            <option value="">choose…</option>
                            {library.map((i) => <option key={i.id} value={i.id}>{i.name}</option>)}
                        </select>
                    </label>
                    <label className="block">
                        <span className="text-muted">start at (seconds)</span>
                        <input type="number" min="0" step="0.5" value={slot.offset_s}
                               onChange={(e) => set({ offset_s: Math.max(0, Number(e.target.value) || 0) })}
                               className="input-field mt-1.5 w-24 py-1.5 min-h-6" />
                    </label>
                    <div>
                        <span className="text-muted" id={`${id}-fit-label`}>crop position</span>
                        <div
                            ref={areaRef}
                            role="application"
                            tabIndex={0}
                            aria-labelledby={`${id}-fit-label`}
                            aria-describedby={`${id}-fit-value`}
                            onPointerDown={onPointerDown}
                            onPointerMove={onPointerMove}
                            onKeyDown={onKeyDown}
                            className="relative mt-1.5 h-24 rounded-input border border-rule2 bg-paper3 touch-none cursor-crosshair focus:outline-none focus-visible:ring-2 focus-visible:ring-[color:var(--color-focus)]"
                        >
                            {picked?.thumb && (
                                <img src={getApiUrl(picked.thumb)} alt="" draggable={false}
                                     className="absolute inset-0 w-full h-full object-cover opacity-60 pointer-events-none rounded-input" />
                            )}
                            <span
                                className="absolute w-3 h-3 -ml-1.5 -mt-1.5 rounded-full border-2 border-brass bg-paper pointer-events-none"
                                style={{ left: `${slot.fit.x * 100}%`, top: `${slot.fit.y * 100}%` }}
                            />
                        </div>
                        <p id={`${id}-fit-value`} className="readout mt-1">
                            {Math.round(slot.fit.x * 100)}% across, {Math.round(slot.fit.y * 100)}% down · drag or arrow keys
                        </p>
                    </div>
                </>
            )}
        </div>
    );
}
