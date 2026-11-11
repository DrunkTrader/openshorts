import React, { useState, useEffect, useCallback, useRef } from 'react';
import { X, Loader2, Crosshair, RotateCcw, AlertCircle, Play, Pause, Columns2, Trash2 } from 'lucide-react';
import { getApiUrl } from '../config';
import { apiJson, apiFetch } from '../lib/api';

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
                setOverrides(Object.fromEntries(
                    Object.entries(salvos).map(([k, v]) => [Number(k), v])
                ));
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
        setOverrides((o) => ({ ...o, [idx]: clamp(fraction) }));
    }, [clamp]);

    const setSplitHalf = useCallback((idx, which, fraction) => {
        setOverrides((o) => {
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
    }, [clamp]);

    const toggleSplit = useCallback((idx, scene) => {
        setOverrides((o) => {
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
    }, [clamp]);

    const resetScene = useCallback((idx) => {
        setOverrides((o) => {
            const next = { ...o };
            delete next[idx];
            return next;
        });
    }, []);

    const adjusted = Object.keys(overrides).length;

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

    return (
        /* Bottom sheet on a phone, centred dialog from sm — same shape as the
           shared Modal so the app has one overlay idiom, not two. */
        <div className="fixed inset-0 z-50 bg-black/80 flex items-end sm:items-center justify-center p-0 sm:p-4">
            <div className="card w-full max-w-3xl max-h-[92vh] sm:max-h-[90vh] flex flex-col rounded-b-none sm:rounded-card animate-sheet-up sm:animate-none">
                <div className="flex items-center justify-between p-4 border-b border-rule">
                    <div className="flex items-center gap-2.5 min-w-0">
                        <Crosshair size={18} className="text-brass shrink-0" />
                        <div className="min-w-0">
                            <h2 className="text-base font-medium text-ink lowercase truncate">reframing</h2>
                            {clipTitle && <p className="text-xs text-muted truncate">{clipTitle}</p>}
                        </div>
                    </div>
                    <button onClick={onClose} className="p-1.5 hover:bg-paper3 rounded-input transition-colors">
                        <X size={18} className="text-muted" />
                    </button>
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
                        <button onClick={onClose} className="btn-quiet py-2 px-4 text-sm">cancel</button>
                        <button
                            onClick={handleSave}
                            disabled={!adjusted || saving}
                            className="btn-primary py-2 px-4 text-sm disabled:opacity-40"
                        >
                            {saving ? <Loader2 size={16} className="animate-spin" /> : null}
                            {saving ? 're-rendering…' : 'apply reframing'}
                        </button>
                    </div>
                </div>
                <div className="sm:hidden safe-bottom" />
            </div>
        </div>
    );
}


// One scene. While it plays, the frame is replaced by the uncropped preview
// seeked to this scene, so the rectangle can be judged against moving pictures
// and sound rather than a single still.
function SceneRow({ scene, value, widthFraction, previewUrl, touched, playing,
                    onPlayToggle, onMoveSingle, onMoveHalf, onToggleSplit, onReset }) {
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

    const win = (centre, label, which) => {
        const leftPct = (centre - widthFraction / 2) * 100;
        return (
            <div
                key={which}
                onMouseDown={startDrag(which)}
                onTouchStart={startDrag(which)}
                className="absolute inset-y-0 border-2 border-brass cursor-ew-resize"
                style={{ left: `${leftPct}%`, width: `${widthFraction * 100}%` }}
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
        <div className="space-y-1.5">
            <div className="flex items-center justify-between text-xs gap-2">
                <div className="flex items-center gap-2 min-w-0">
                    <button
                        onClick={onPlayToggle}
                        className="flex items-center gap-1 text-ink2 hover:text-brass transition-colors"
                        title="play this scene with sound"
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
                        className={`flex items-center gap-1 transition-colors ${
                            isSplit ? 'text-brass' : 'text-muted hover:text-ink2'}`}
                        title="stack two regions instead of one window"
                    >
                        <Columns2 size={12} /> split
                    </button>
                    {touched ? (
                        <button onClick={onReset} className="flex items-center gap-1 text-brass hover:underline">
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
        <section className="border border-rule rounded-input" aria-labelledby="slot-split-title">
            <h3 id="slot-split-title" className="text-sm font-medium text-ink lowercase">
                <button
                    type="button"
                    onClick={() => setOpen((o) => !o)}
                    aria-expanded={open}
                    aria-controls="slot-split-body"
                    className="w-full flex items-center gap-2 p-3 text-left"
                >
                    <Columns2 size={14} className="text-brass" /> split with clip
                </button>
            </h3>
            {open && (
                <div id="slot-split-body" className="p-3 pt-0 space-y-4">
                    <p className="text-xs text-muted leading-relaxed">
                        Stack this clip with a video of your own, such as gameplay. Pick what
                        each half shows, then drag the picture (or use the arrow keys) to
                        choose which part stays in frame.
                    </p>

                    <div className="grid sm:grid-cols-2 gap-4">
                        <SlotControl id="top" label="top half" slot={top} onChange={setTop} library={library} />
                        <SlotControl id="bottom" label="bottom half" slot={bottom} onChange={setBottom} library={library} />
                    </div>

                    <fieldset className="flex flex-wrap items-center gap-4 text-xs">
                        <legend className="sr-only">split settings</legend>
                        <div role="radiogroup" aria-label="split ratio" className="flex gap-1">
                            {['50/50', '60/40'].map((r) => (
                                <button
                                    key={r}
                                    type="button"
                                    role="radio"
                                    aria-checked={ratio === r}
                                    onClick={() => setRatio(r)}
                                    className={`px-2.5 py-1 rounded-input border ${
                                        ratio === r ? 'border-brass text-brass' : 'border-rule text-muted'}`}
                                >
                                    {r}
                                </button>
                            ))}
                        </div>
                        <label className="flex items-center gap-1.5">
                            <span className="text-muted">audio</span>
                            <select value={audio} onChange={(e) => setAudio(e.target.value)}
                                    className="bg-paper border border-rule rounded-input px-1.5 py-1">
                                <option value="source">this clip</option>
                                <option value="upload">uploaded video</option>
                                <option value="mix">mix of both</option>
                            </select>
                        </label>
                        <label className="flex items-center gap-1.5">
                            <input type="checkbox" checked={loop} onChange={(e) => setLoop(e.target.checked)} />
                            <span>loop uploaded video</span>
                        </label>
                    </fieldset>

                    <div className="space-y-2">
                        <label className="text-xs text-muted block" htmlFor="slot-upload-input">
                            add a video (MP4, MOV, WebM or MKV)
                        </label>
                        <input id="slot-upload-input" type="file" accept=".mp4,.mov,.webm,.mkv,video/*"
                               disabled={uploading}
                               onChange={(e) => { upload(e.target.files?.[0]); e.target.value = ''; }}
                               className="text-xs" />
                        {uploading && (
                            <p role="status" className="text-xs text-muted flex items-center gap-1.5">
                                <Loader2 size={12} className="animate-spin" /> uploading…
                            </p>
                        )}
                        {libError && <p role="alert" className="text-xs text-warn">{libError}</p>}
                        {library.length === 0 && !libError && (
                            <p className="text-xs text-muted">Your library is empty.</p>
                        )}
                        <ul className="space-y-1">
                            {library.map((item) => (
                                <li key={item.id} className="flex items-center gap-2 text-xs">
                                    {item.thumb && (
                                        <img src={getApiUrl(item.thumb)} alt="" className="w-10 h-7 object-cover rounded-input" />
                                    )}
                                    <span className="truncate flex-1">{item.name}</span>
                                    <span className="readout text-muted">{fmt(item.duration || 0)}</span>
                                    <button type="button" onClick={() => remove(item.id)}
                                            aria-label={`delete ${item.name}`}
                                            className="p-1 text-muted hover:text-warn">
                                        <Trash2 size={13} />
                                    </button>
                                </li>
                            ))}
                        </ul>
                    </div>

                    {error && (
                        <p role="alert" className="flex items-start gap-2 text-xs text-warn">
                            <AlertCircle size={14} className="shrink-0 mt-0.5" /> {error}
                        </p>
                    )}
                    <div className="flex items-center gap-3">
                        <button type="button" onClick={apply} disabled={busy || missing}
                                className="btn-primary py-2 px-4 text-sm disabled:opacity-40">
                            {busy ? <Loader2 size={16} className="animate-spin" /> : null}
                            {busy ? 'rendering the split…' : 'apply split'}
                        </button>
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
        <div className="space-y-2 text-xs">
            <label className="block">
                <span className="text-muted">{label}</span>
                <select value={slot.kind} onChange={(e) => set({ kind: e.target.value })}
                        className="mt-1 block w-full bg-paper border border-rule rounded-input px-1.5 py-1">
                    <option value="source">this clip</option>
                    <option value="upload">uploaded video</option>
                </select>
            </label>
            {slot.kind === 'upload' && (
                <>
                    <label className="block">
                        <span className="text-muted">video for the {label}</span>
                        <select value={slot.upload_id} onChange={(e) => set({ upload_id: e.target.value })}
                                className="mt-1 block w-full bg-paper border border-rule rounded-input px-1.5 py-1">
                            <option value="">choose…</option>
                            {library.map((i) => <option key={i.id} value={i.id}>{i.name}</option>)}
                        </select>
                    </label>
                    <label className="block">
                        <span className="text-muted">start at (seconds)</span>
                        <input type="number" min="0" step="0.5" value={slot.offset_s}
                               onChange={(e) => set({ offset_s: Math.max(0, Number(e.target.value) || 0) })}
                               className="mt-1 block w-24 bg-paper border border-rule rounded-input px-1.5 py-1" />
                    </label>
                    <div>
                        <span className="text-muted" id={`${id}-fit-label`}>crop position</span>
                        <div
                            ref={areaRef}
                            role="application"
                            tabIndex={0}
                            aria-labelledby={`${id}-fit-label`}
                            aria-description="Drag, or use the arrow keys, to move the crop"
                            aria-valuetext={`${Math.round(slot.fit.x * 100)}% across, ${Math.round(slot.fit.y * 100)}% down`}
                            onPointerDown={onPointerDown}
                            onPointerMove={onPointerMove}
                            onKeyDown={onKeyDown}
                            className="relative mt-1 h-24 rounded-input border border-rule bg-paper3 touch-none cursor-crosshair focus:outline-none focus-visible:ring-2 focus-visible:ring-brass"
                        >
                            {picked?.thumb && (
                                <img src={getApiUrl(picked.thumb)} alt="" draggable={false}
                                     className="absolute inset-0 w-full h-full object-cover opacity-60 pointer-events-none" />
                            )}
                            <span
                                className="absolute w-3 h-3 -ml-1.5 -mt-1.5 rounded-full border-2 border-brass bg-paper pointer-events-none"
                                style={{ left: `${slot.fit.x * 100}%`, top: `${slot.fit.y * 100}%` }}
                            />
                        </div>
                    </div>
                </>
            )}
        </div>
    );
}
