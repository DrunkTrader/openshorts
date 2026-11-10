import { STAGES } from '../lib/jobStage';

// Live stage timeline: queued -> transcribing -> scoring -> rendering -> done.
// The current step carries aria-current="step"; finished steps are filled.
// compact: narrow column, only the current step keeps its visible label.
export default function JobTimeline({ info, compact = false }) {
  const { index, failed, detail, pct } = info;
  return (
    <div className="mb-4">
      <ol className="flex items-start" aria-label="Job progress">
        {STAGES.map((s, i) => {
          const done = i < index || (s.id === 'done' && index === 4);
          const current = i === index && !done;
          return (
            <li
              key={s.id}
              aria-current={current ? 'step' : undefined}
              className="flex-1 min-w-0 relative flex flex-col items-center text-center"
            >
              {i > 0 && (
                <span
                  aria-hidden="true"
                  className={`absolute top-[7px] right-1/2 w-full h-px transition-colors duration-200 ${i <= index ? 'bg-ink' : 'bg-[var(--color-rule-2)]'}`}
                />
              )}
              <span
                aria-hidden="true"
                className={`relative z-10 w-[15px] h-[15px] rounded-full border transition-colors duration-200 ${
                  done ? 'bg-ink border-ink'
                    : current ? (failed ? 'bg-danger border-danger' : 'bg-brass border-brass ring-4 ring-brass/20')
                      : 'bg-paper2 border-rule2'}`}
              />
              <span className={`mt-2 text-[11px] sm:text-xs leading-tight transition-colors duration-200 ${compact && !current && !(s.id === 'done' && index === 4) ? 'sr-only sm:not-sr-only' : ''} ${current ? 'text-ink font-medium' : done ? 'text-ink2' : 'text-muted'}`}>
                {s.label}
              </span>
            </li>
          );
        })}
      </ol>
      <p className={`mt-3 text-sm tabular ${failed ? 'text-danger' : 'text-ink2'}`}>{detail}</p>
      {pct != null && !failed && (
        <div className="mt-2 h-1 rounded-full bg-paper3 overflow-hidden" role="progressbar" aria-label="Transcription" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
          <div className="h-full bg-brass transition-[width] duration-200 ease-out" style={{ width: `${pct}%` }} />
        </div>
      )}
    </div>
  );
}
