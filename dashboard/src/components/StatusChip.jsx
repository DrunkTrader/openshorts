import Chip from './ui/Chip';

const TONE = { queued: 'neutral', transcribing: 'accent', scoring: 'accent', rendering: 'accent', done: 'ok' };
const LABEL = { queued: 'Queued', transcribing: 'Transcribing', scoring: 'Scoring', rendering: 'Rendering', done: 'Done' };

// One pill for the job state. The dot pulses while work is running (the global
// reduced-motion rule stills it). aria-live announces each stage change.
export default function StatusChip({ stage, failed = false, className = '' }) {
  const live = !failed && stage !== 'done' && stage !== 'queued';
  return (
    <Chip
      tone={failed ? 'danger' : TONE[stage] || 'neutral'}
      className={`transition-colors duration-200 ${className}`}
      role="status"
      aria-live="polite"
      data-stage={failed ? 'failed' : stage}
    >
      <span aria-hidden="true" className={`inline-block w-1.5 h-1.5 rounded-full bg-current ${live ? 'animate-pulse' : ''}`} />
      {failed ? 'Failed' : LABEL[stage] || stage}
    </Chip>
  );
}
