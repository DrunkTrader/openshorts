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
