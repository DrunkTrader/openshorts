// Status pill. tone: ok | warn | danger | accent | neutral
const TONES = { ok: 'badge-ok', warn: 'badge-warn', danger: 'badge-danger', accent: 'badge-brass' };

export default function Chip({ tone = 'neutral', className = '', children, ...props }) {
  const base = TONES[tone] || 'badge-ok text-muted bg-paper3';
  const neutral = tone === 'neutral' ? '!text-muted !bg-paper3' : '';
  return <span className={`${base} ${neutral} ${className}`} {...props}>{children}</span>;
}
