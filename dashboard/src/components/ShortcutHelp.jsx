import { SHORTCUTS } from '../lib/shortcuts';
import Modal from './ui/Modal';

// The `?` dialog. Modal supplies the trap, Escape and focus return; the
// editor's own button is the fallback target when `?` was pressed with focus
// on the page body.
export default function ShortcutHelp({ isOpen, onClose, scope, returnFocusRef }) {
  const rows = SHORTCUTS.filter((s) => s.scope === scope || s.scope === 'both');
  return (
    <Modal isOpen={isOpen} onClose={onClose} returnFocusRef={returnFocusRef} size="md" eyebrow="KEYBOARD" title="shortcuts">
      <p className="text-xs text-muted mb-3">Ignored while you are typing in a field.</p>
      <dl className="divide-y divide-[color:var(--color-rule)]">
        {rows.map((s) => (
          <div key={s.label} className="flex items-start justify-between gap-4 py-2.5">
            <dt className="text-sm text-ink2">{s.label}</dt>
            <dd className="flex flex-wrap justify-end gap-1 shrink-0">
              {s.keys.map((k) => (
                <kbd key={k} className="readout px-1.5 py-0.5 rounded-sm border border-rule2 bg-paper3 text-ink">{k}</kbd>
              ))}
            </dd>
          </div>
        ))}
      </dl>
    </Modal>
  );
}
