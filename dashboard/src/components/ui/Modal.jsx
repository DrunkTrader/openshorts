import { useEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';

/**
 * The single modal shell for the app (design.md).
 * Plain dark overlay (no backdrop-blur), hairline paper2 panel.
 *
 * Props:
 *  - isOpen / onClose
 *  - title (string, rendered lowercase serif) — optional
 *  - eyebrow (string, mono UPPERCASE micro label above title) — optional
 *  - size: 'sm' | 'md' | 'lg' | 'xl' (max width; default 'md')
 *  - children: body content
 *  - footer: optional node pinned under the body
 *  - hideClose: hide the X button
 */
const SIZES = {
  sm: 'max-w-sm',
  md: 'max-w-md',
  lg: 'max-w-2xl',
  xl: 'max-w-5xl',
};

// Open modals, topmost last: Escape closes only the top one and Tab cycles
// inside it. The page behind is inert and scroll-locked while any is open, and
// so is every dialog under the top one.
const stack = [];

const FOCUSABLE = 'a[href], button, input, select, textarea, summary, [tabindex], [contenteditable="true"]';

/** Tab / Shift+Tab wrap inside `box`; focus that escaped is pulled back in. */
export function trapTab(e, box) {
  if (e.key !== 'Tab' || !box) return;
  const items = [...box.querySelectorAll(FOCUSABLE)].filter((el) => (
    !el.disabled && el.tabIndex >= 0 && el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden'
    && !(el.type === 'radio' && !el.checked && box.querySelector(`input[type=radio][name="${el.name}"]:checked`))
  ));
  if (!items.length) { e.preventDefault(); box.focus(); return; }
  const first = items[0];
  const last = items[items.length - 1];
  const at = document.activeElement;
  if (!box.contains(at) || at === box) { e.preventDefault(); (e.shiftKey ? last : first).focus(); }
  else if (e.shiftKey && at === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && at === last) { e.preventDefault(); first.focus(); }
}

export default function Modal({ isOpen, onClose, returnFocusRef, title, eyebrow, size = 'md', children, footer, hideClose = false }) {
  const panelRef = useRef(null);
  const overlayRef = useRef(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  // Captured during render, before any child effect can move focus (a child
  // that focuses itself would otherwise become the "opener" and vanish on close).
  const openerRef = useRef(null);
  if (isOpen && !openerRef.current && typeof document !== 'undefined') openerRef.current = document.activeElement;
  if (!isOpen) openerRef.current = null;

  // Escape closes; focus moves into the dialog and returns to the opener.
  useEffect(() => {
    if (!isOpen) return undefined;
    const opener = openerRef.current;
    const fallback = returnFocusRef?.current;
    panelRef.current?.focus();
    const onKey = (e) => {
      if (e.key === 'Escape' && closeRef.current) { e.stopPropagation(); closeRef.current(); }
      else trapTab(e, panelRef.current);
    };
    const token = {};
    stack[stack.length - 1]?.overlay?.setAttribute('inert', '');
    token.overlay = overlayRef.current;
    stack.push(token);
    const root = document.getElementById('root');
    root?.setAttribute('inert', '');
    document.body.style.overflow = 'hidden';
    const onKeyTop = (e) => { if (stack[stack.length - 1] === token) onKey(e); };
    document.addEventListener('keydown', onKeyTop);
    return () => {
      document.removeEventListener('keydown', onKeyTop);
      stack.splice(stack.indexOf(token), 1);
      stack[stack.length - 1]?.overlay?.removeAttribute('inert');
      if (!stack.length) {
        root?.removeAttribute('inert');
        document.body.style.overflow = '';
      }
      const back = opener instanceof HTMLElement && document.contains(opener) && opener !== document.body ? opener : fallback;
      if (back instanceof HTMLElement) back.focus();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen]);

  if (!isOpen) return null;

  // Portalled: a transformed or overflow-hidden ancestor (a hovered card) must
  // not become the containing block of `fixed`.
  return createPortal(
    /* Phone: a bottom sheet — anchored to the thumb, edge to edge, and free to
       run taller than a centred dialog whose 4px side margins wasted the only
       width there was. From sm up it is the centred dialog it always was. */
    <div
      ref={overlayRef}
      className="fixed inset-0 z-[100] flex items-end sm:items-center justify-center bg-black/70 p-0 sm:p-4 animate-fade"
      onMouseDown={(e) => { if (e.target === e.currentTarget && onClose) onClose(); }}
      role="dialog"
      aria-modal="true"
      aria-label={title || eyebrow || 'dialog'}
    >
      <div
        ref={panelRef}
        tabIndex={-1}
        className={`outline-none card relative w-full ${SIZES[size] || SIZES.md} max-h-[92vh] sm:max-h-[90vh] flex flex-col
          rounded-b-none sm:rounded-card animate-sheet-up sm:animate-none`}
      >
        {/* Grab handle: the affordance that says "this sheet closes downward". */}
        <div className="sm:hidden pt-2.5 pb-1 flex justify-center shrink-0" aria-hidden="true">
          <span className="w-9 h-1 rounded-full bg-[color:var(--color-rule-2)]" />
        </div>
        {!hideClose && onClose && (
          <button
            onClick={onClose}
            aria-label="close"
            className="absolute top-3 right-3 sm:top-4 sm:right-4 z-10 p-2 rounded-full text-muted hover:text-ink hover:bg-paper3 transition-colors"
          >
            <X size={18} />
          </button>
        )}
        {(title || eyebrow) && (
          <div className="px-4 sm:px-6 pt-3 sm:pt-6 pb-4 border-b border-rule shrink-0">
            {eyebrow && <p className="eyebrow mb-1.5">{eyebrow}</p>}
            {title && <h2 className="font-display lowercase text-xl sm:text-2xl text-ink leading-tight break-words pr-10">{title}</h2>}
          </div>
        )}
        <div className="px-4 sm:px-6 py-5 overflow-y-auto overscroll-contain custom-scrollbar grow">
          {children}
        </div>
        {footer && (
          <div className="px-4 sm:px-6 py-4 border-t border-rule shrink-0 safe-bottom">
            {footer}
          </div>
        )}
        {!footer && <div className="sm:hidden safe-bottom" />}
      </div>
    </div>,
    document.body,
  );
}
