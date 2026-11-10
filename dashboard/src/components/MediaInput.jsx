import React, { useState, useEffect, useRef } from 'react';
import { Upload, FileVideo, X, Info, Loader2, ChevronDown, ArrowRight } from 'lucide-react';
import { track } from '../lib/analytics';
import { getApiUrl } from '../config';
import SourceCost from './SourceCost';
import Button from './ui/Button';
import Card from './ui/Card';
import Chip from './ui/Chip';

const SUPPORTED_PLATFORMS = [
    'YouTube', 'Vimeo', 'TikTok', 'X / Twitter', 'Twitch', 'Kick',
    'Facebook', 'Instagram', 'Dailymotion', 'Reddit', 'Streamable',
];

// Mirrors MIN_SOURCE_SECONDS in app.py (default 45, env-overridable): shorter sources are rejected with a
// 400 after the whole file was uploaded. Checking the duration in the browser
// says so the moment the file is picked (a 27 s upload used to end in a bare
// "That run failed").
const MIN_SOURCE_SECONDS = 45;

const store = {
    get: (k, d) => { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

// Duration of a local video file in seconds, or null when the browser cannot
// read it (unsupported codec): then the server stays the judge.
const readVideoDuration = (file) => new Promise((resolve) => {
    try {
        const url = URL.createObjectURL(file);
        const v = document.createElement('video');
        v.preload = 'metadata';
        const done = (d) => { URL.revokeObjectURL(url); resolve(d); };
        v.onloadedmetadata = () => done(Number.isFinite(v.duration) ? v.duration : null);
        v.onerror = () => done(null);
        setTimeout(() => done(null), 8000);
        v.src = url;
    } catch { resolve(null); }
});

const FORMATS = [
    { value: 'vertical', label: '9:16', hint: 'Shorts · Reels · TikTok', w: 9, h: 16 },
    { value: 'square', label: '1:1', hint: 'Feed posts', w: 13, h: 13 },
    { value: 'horizontal', label: '16:9', hint: 'Keep landscape · YouTube', w: 18, h: 10 },
];

export default function MediaInput({ onProcess, isProcessing }) {
    const [youtubeUrlEnabled, setYoutubeUrlEnabled] = useState(true);
    const [url, setUrl] = useState('');
    const [file, setFile] = useState(null);
    // One surface: a picked file wins, otherwise the field holds a link.
    const mode = file ? 'file' : 'url';
    const [error, setError] = useState(null); // {field: 'url' | 'ack', text}
    const [dragging, setDragging] = useState(false);
    const urlRef = useRef(null);
    const ackRef = useRef(null);
    const fileRef = useRef(null);
    const [fileSeconds, setFileSeconds] = useState(null);
    const fileTooShort = fileSeconds != null && fileSeconds < MIN_SOURCE_SECONDS;
    const [acknowledged, setAcknowledged] = useState(false);
    // {start, end} in seconds when the user picked a stretch under the cost line.
    const [range, setRange] = useState(null);
    const [outputFormat, setOutputFormat] = useState('vertical'); // vertical | horizontal | square
    const [showInfo, setShowInfo] = useState(false);
    // Advanced generation controls — empty string means "let the AI decide",
    // which keeps the default pipeline behavior untouched.
    const [showAdvanced, setShowAdvanced] = useState(false);
    const [targetClips, setTargetClips] = useState(() => store.get('os_target_clips', ''));
    const [clipMinSeconds, setClipMinSeconds] = useState(() => store.get('os_clip_min', ''));
    const [clipMaxSeconds, setClipMaxSeconds] = useState(() => store.get('os_clip_max', ''));
    // Auto-hook: burn the AI hook text into every clip. On by default; the
    // choice persists so turning it off sticks across sessions.
    const [autoHook, setAutoHook] = useState(() => {
        try { return localStorage.getItem('os_auto_hook') !== '0'; } catch { return true; }
    });
    const [autoHookStyle, setAutoHookStyle] = useState(() => {
        // v2 key: the default became 'pill'; an old saved 'classic' was just the old default.
        try { return localStorage.getItem('os_auto_hook_style_v2') || 'pill'; } catch { return 'pill'; }
    });
    // Layout: 'auto' lets the AI pick per video (server default); the others
    // force one on so a podcast host who knows what they uploaded doesn't
    // depend on the detector, and 'none' keeps the plain single crop.
    const [layout, setLayout] = useState(() => {
        try { return localStorage.getItem('os_layout') || 'auto'; } catch { return 'auto'; }
    });
    const infoRef = useRef(null);

    // Advanced options persist as they change, not only on submit.
    useEffect(() => {
        store.set('os_target_clips', targetClips);
        store.set('os_clip_min', clipMinSeconds);
        store.set('os_clip_max', clipMaxSeconds);
        store.set('os_layout', layout);
        store.set('os_auto_hook', autoHook ? '1' : '0');
        store.set('os_auto_hook_style_v2', autoHookStyle);
    }, [targetClips, clipMinSeconds, clipMaxSeconds, layout, autoHook, autoHookStyle]);

    // Close the compatibility popover on any outside click.
    useEffect(() => {
        if (!showInfo) return;
        const onClick = (e) => {
            if (infoRef.current && !infoRef.current.contains(e.target)) setShowInfo(false);
        };
        document.addEventListener('mousedown', onClick);
        return () => document.removeEventListener('mousedown', onClick);
    }, [showInfo]);

    useEffect(() => {
        let cancelled = false;
        setFileSeconds(null);
        if (!file) return undefined;
        readVideoDuration(file).then((d) => {
            if (cancelled) return;
            setFileSeconds(d);
            if (d != null && d < MIN_SOURCE_SECONDS) {
                track('SourceTooShort', { props: { seconds: String(Math.round(d)) } });
            }
        });
        return () => { cancelled = true; };
    }, [file]);

    useEffect(() => {
        fetch(getApiUrl('/api/config'))
            .then((r) => r.ok ? r.json() : null)
            .then((cfg) => {
                if (cfg && cfg.youtubeUrlEnabled === false) {
                    setYoutubeUrlEnabled(false);
                }
            })
            .catch(() => {});
    }, []);

    // A link pasted in the landing hero: preload it here so the user picks up
    // where they left off. Not auto-submitted — the rights attestation below
    // has to be ticked by the user.
    useEffect(() => {
        let pending = null;
        try {
            pending = localStorage.getItem('os_pending_url');
            if (pending) localStorage.removeItem('os_pending_url');
        } catch { /* ignore */ }
        if (pending) {
            setUrl(pending);
        }
    }, []);

    const fail = (field, text, ref) => {
        setError({ field, text });
        ref.current?.focus();
    };

    const handleSubmit = (e) => {
        e.preventDefault();
        if (mode === 'url' && !/^https?:\/\/\S+\.\S+$/.test(url.trim())) {
            fail('url', 'That is not a link. Paste a full address starting with https://, or drop a video file.', urlRef);
            return;
        }
        if (mode === 'file' && fileTooShort) return;
        if (!acknowledged) {
            fail('ack', 'Tick the box to confirm you have the rights to this video.', ackRef);
            return;
        }
        setError(null);
        const advanced = {
            targetClips: targetClips || null,
            clipMinSeconds: clipMinSeconds || null,
            clipMaxSeconds: clipMaxSeconds || null,
            autoHook,
            autoHookStyle,
            layout,
            sourceStart: range ? Math.round(range.start) : null,
            sourceEnd: range ? Math.round(range.end) : null,
        };
        if (mode === 'url' && url) {
            onProcess({ type: 'url', payload: url.trim(), acknowledged: true, outputFormat, ...advanced });
        } else if (mode === 'file' && file && !fileTooShort) {
            onProcess({ type: 'file', payload: file, acknowledged: true, outputFormat, ...advanced });
        }
    };

    const pickFile = (f) => {
        if (!f) return;
        setFile(f);
        setError(null);
    };

    const handleDrop = (e) => {
        e.preventDefault();
        setDragging(false);
        pickFile(e.dataTransfer.files?.[0]);
    };

    const fmtSize = (b) => (b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : b >= 1e6 ? `${Math.round(b / 1e6)} MB` : `${Math.max(1, Math.round(b / 1e3))} KB`);
    const empty = mode === 'url' && !url.trim();
    const advancedSet = targetClips || clipMinSeconds || clipMaxSeconds || !autoHook || layout !== 'auto';

    return (
        <Card
            className={`p-4 sm:p-6 text-left animate-fade transition-colors duration-200 ${dragging ? '!border-ink' : ''}`}
            onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
            onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget)) setDragging(false); }}
            onDrop={handleDrop}
        >
            <form onSubmit={handleSubmit} noValidate>
                <div data-tutorial="source-tabs">
                    <label htmlFor="source-url" className="eyebrow block mb-2">
                        {file ? 'Your video' : youtubeUrlEnabled ? 'Paste a link or drop a video' : 'Drop a video'}
                    </label>
                    <div data-tutorial="drop-zone">
                        {file ? (
                            <div className="flex items-center gap-3 rounded-input border border-rule2 bg-paper2 pl-3 pr-1.5 py-1.5 min-w-0 animate-fade" data-testid="file-chip">
                                <FileVideo size={18} className="shrink-0 text-muted" aria-hidden="true" />
                                <span className="font-medium text-sm text-ink truncate min-w-0 flex-1">{file.name}</span>
                                <span className="readout tabular shrink-0">
                                    {fmtSize(file.size)}{fileSeconds != null ? ` · ${Math.floor(Math.round(fileSeconds) / 60)}:${String(Math.round(fileSeconds) % 60).padStart(2, '0')}` : ''}
                                </span>
                                <button
                                    type="button"
                                    onClick={() => { setFile(null); if (fileRef.current) fileRef.current.value = ''; }}
                                    aria-label={`Remove ${file.name}`}
                                    className="p-2 text-muted hover:text-ink hover:bg-paper3 rounded-full transition-colors duration-150 shrink-0"
                                >
                                    <X size={16} />
                                </button>
                            </div>
                        ) : (
                            <div className="flex flex-col sm:flex-row gap-2">
                                {youtubeUrlEnabled && (
                                    <div className="relative flex-1 min-w-0">
                                        <input
                                            id="source-url"
                                            ref={urlRef}
                                            type="text"
                                            inputMode="url"
                                            autoComplete="off"
                                            value={url}
                                            onChange={(e) => { setUrl(e.target.value); if (error?.field === 'url') setError(null); }}
                                            placeholder="https://... paste a video link"
                                            aria-invalid={error?.field === 'url' || undefined}
                                            aria-describedby={error?.field === 'url' ? 'source-error' : undefined}
                                            className="input-field pr-11"
                                        />
                                        <div className="absolute inset-y-0 right-2 flex items-center" ref={infoRef}>
                                            <button
                                                type="button"
                                                onClick={() => setShowInfo((v) => !v)}
                                                aria-label="Supported platforms"
                                                aria-expanded={showInfo}
                                                className="p-1.5 text-muted hover:text-ink transition-colors duration-150"
                                            >
                                                <Info size={16} />
                                            </button>
                                {showInfo && (
                                    <div className="absolute right-0 top-full mt-2 w-64 z-20 card p-4 text-left animate-fade">
                                        <p className="eyebrow mb-2">Paste a link from</p>
                                        <div className="flex flex-wrap gap-1.5">
                                            {SUPPORTED_PLATFORMS.map((p) => (
                                                <Chip key={p} tone="neutral">{p}</Chip>
                                            ))}
                                        </div>
                                        <p className="text-xs text-muted mt-2.5 leading-relaxed">
                                            …and 1,000+ more sites. If a link has a public video, we can usually fetch it.
                                        </p>
                                    </div>
                                )}
                                        </div>
                                    </div>
                                )}
                                <Button variant="ghost" onClick={() => fileRef.current?.click()} className="shrink-0">
                                    <Upload size={16} aria-hidden="true" />
                                    Upload file
                                </Button>
                            </div>
                        )}
                        <input
                            ref={fileRef}
                            type="file"
                            accept="video/*"
                            tabIndex={-1}
                            aria-label="Video file"
                            onChange={(e) => pickFile(e.target.files?.[0])}
                            className="hidden"
                        />
                        <p className="readout mt-2">
                            {dragging ? 'Drop to add this video' : `MP4, MOV up to 500MB · at least ${MIN_SOURCE_SECONDS}s long`}
                        </p>
                    </div>
                    {file && fileTooShort && (
                        <p className="text-danger text-sm mt-3" role="alert">
                            This video is {Math.round(fileSeconds)}s long. Clip generation needs at least {MIN_SOURCE_SECONDS}s
                            of footage to cut from: it already is a short. Pick a longer video.
                        </p>
                    )}
                    {error && (
                        <p id="source-error" className="text-danger text-sm mt-3" role="alert">{error.text}</p>
                    )}
                </div>

                <SourceCost
                    mode={mode}
                    url={url}
                    fileSeconds={fileSeconds}
                    range={range}
                    onRangeChange={setRange}
                />

                {/* Output format: one compact row, the selected one names its destinations */}
                <div className="mt-4" data-tutorial="output-format" role="group" aria-label="Output format">
                    <p className="eyebrow mb-2">
                        Output format <span className="normal-case tracking-normal opacity-80">· {FORMATS.find((f) => f.value === outputFormat).hint}</span>
                    </p>
                    <div className="grid grid-cols-3 gap-2">
                        {FORMATS.map((f) => {
                            const active = outputFormat === f.value;
                            return (
                                <button
                                    key={f.value}
                                    type="button"
                                    aria-pressed={active}
                                    onClick={() => setOutputFormat(f.value)}
                                    className={`h-11 rounded-input border flex items-center justify-center gap-2.5 transition-colors duration-150
                                        ${active ? 'border-ink bg-paper3 text-ink' : 'border-rule2 text-muted hover:border-ink hover:text-ink'}`}
                                >
                                    <span
                                        aria-hidden="true"
                                        className="rounded-[2px] border-[1.5px] border-current shrink-0"
                                        style={{ width: `${f.w}px`, height: `${f.h}px` }}
                                    />
                                    <span className="font-mono text-sm leading-none">{f.label}</span>
                                </button>
                            );
                        })}
                    </div>
                </div>

                {/* Advanced generation controls — collapsed by default; blank = AI decides */}
                <div className="mt-4">
                    <button
                        type="button"
                        onClick={() => setShowAdvanced((v) => !v)}
                        aria-expanded={showAdvanced}
                        className="flex items-center gap-1.5 text-xs text-muted hover:text-ink rounded-sm transition-colors duration-150"
                    >
                        <ChevronDown size={14} className={`transition-transform ${showAdvanced ? 'rotate-180' : ''}`} />
                        Advanced options
                        {advancedSet && <span className="text-ink" aria-label="customised">·</span>}
                    </button>
                    {showAdvanced && (
                        /* Stacked on a phone: three number fields side by side leaves
                           ~100px each, which crushes both label and value. */
                        <div className="mt-3 grid grid-cols-1 sm:grid-cols-3 gap-3 sm:gap-2 animate-fade">
                            <div>
                                <p className="eyebrow mb-1.5">clips to aim for</p>
                                <input
                                    type="number" min="1" max="15" step="1"
                                    value={targetClips}
                                    onChange={(e) => setTargetClips(e.target.value)}
                                    placeholder="auto"
                                    className="input-field"
                                />
                            </div>
                            <div>
                                <p className="eyebrow mb-1.5">min length (s)</p>
                                <input
                                    type="number" min="5" max="175" step="1"
                                    value={clipMinSeconds}
                                    onChange={(e) => setClipMinSeconds(e.target.value)}
                                    placeholder="15"
                                    className="input-field"
                                />
                            </div>
                            <div>
                                <p className="eyebrow mb-1.5">max length (s)</p>
                                <input
                                    type="number" min="10" max="180" step="1"
                                    value={clipMaxSeconds}
                                    onChange={(e) => setClipMaxSeconds(e.target.value)}
                                    placeholder="60"
                                    className="input-field"
                                />
                            </div>
                            <p className="col-span-1 sm:col-span-3 text-[11px] leading-relaxed text-muted">
                                Targets, not guarantees: the AI returns fewer clips when the
                                material doesn't hold them. Leave blank to let it decide.
                            </p>
                            <div className="col-span-1 sm:col-span-3 flex flex-wrap items-center justify-between gap-3 pt-3 sm:pt-1 border-t border-rule">
                                <span className="text-xs text-ink2">vertical layout</span>
                                <select
                                    value={layout}
                                    onChange={(e) => setLayout(e.target.value)}
                                    className="input-field !w-auto text-xs py-1.5"
                                    aria-label="vertical layout"
                                >
                                    <option value="auto">Auto (AI picks per video)</option>
                                    <option value="split">Two speakers stacked</option>
                                    <option value="screencast">Screen over presenter</option>
                                    <option value="none">Single crop only</option>
                                </select>
                            </div>
                            <div className="col-span-1 sm:col-span-3 flex flex-wrap items-center justify-between gap-3 pt-3 sm:pt-1 border-t border-rule">
                                <label className="flex items-center gap-2 text-xs text-ink2 cursor-pointer select-none">
                                    <input
                                        type="checkbox"
                                        checked={autoHook}
                                        onChange={(e) => setAutoHook(e.target.checked)}
                                        className="w-4 h-4 shrink-0 accent-[var(--color-accent)] cursor-pointer"
                                    />
                                    auto hook titles on clips
                                </label>
                                {autoHook && (
                                    <select
                                        value={autoHookStyle}
                                        onChange={(e) => setAutoHookStyle(e.target.value)}
                                        className="input-field !w-auto text-xs py-1.5"
                                    >
                                        <option value="pill">Pills</option>
                                        <option value="classic">Classic</option>
                                        <option value="dark">Dark</option>
                                        <option value="yellow">Yellow</option>
                                        <option value="red">Red</option>
                                        <option value="outline">Outline</option>
                                        <option value="outline_yellow">Outline+</option>
                                    </select>
                                )}
                                {autoHook && (
                                    <p className="w-full text-[11px] leading-relaxed text-muted">
                                        You can change each clip&apos;s hook text, style, position and size
                                        afterwards with its hook button.
                                    </p>
                                )}
                            </div>
                        </div>
                    )}
                </div>

                <label className="flex items-start gap-2.5 mt-4 text-left text-[13px] sm:text-xs leading-relaxed text-muted cursor-pointer select-none">
                    <input
                        ref={ackRef}
                        type="checkbox"
                        checked={acknowledged}
                        onChange={(e) => { setAcknowledged(e.target.checked); if (error?.field === 'ack') setError(null); }}
                        className="mt-0.5 w-4 h-4 shrink-0 accent-[var(--color-accent)] cursor-pointer"
                    />
                    <span>
                        I confirm I own this content or have the rights to process it. I am responsible for any content I submit. See our <a href="/terms" target="_blank" rel="noopener noreferrer" className="text-ink2 underline underline-offset-2 hover:text-brass transition-colors" onClick={(e) => e.stopPropagation()}>Terms</a> and <a href="/privacy" target="_blank" rel="noopener noreferrer" className="text-ink2 underline underline-offset-2 hover:text-brass transition-colors" onClick={(e) => e.stopPropagation()}>Privacy Policy</a>.
                    </span>
                </label>

                <Button
                    type="submit"
                    data-tutorial="generate"
                    disabled={isProcessing || empty || fileTooShort}
                    aria-disabled={isProcessing || empty || fileTooShort}
                    className="w-full mt-4"
                >
                    {isProcessing ? (
                        <>
                            <Loader2 size={16} className="animate-spin" />
                            Processing video...
                        </>
                    ) : (
                        <>
                            Create clips
                            <ArrowRight size={16} aria-hidden="true" />
                        </>
                    )}
                </Button>
            </form>
        </Card>
    );
}
