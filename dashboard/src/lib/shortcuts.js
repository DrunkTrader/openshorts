import { useEffect, useRef } from 'react';
import { trapTab } from './trapTab';

// Keyboard shortcuts shared by the two editors. One window listener per
// editor; the map is read through a ref so handlers always see current state.

// Keys that still work while an input has focus: they are not characters.
const ALWAYS = new Set(['escape', 'mod+enter']);
const TYPING = 'input, textarea, select, [contenteditable="true"]';
// Space on these activates them; stealing it would break the button.
const OWN_SPACE = 'button, a[href], summary, [role="radio"], [role="checkbox"]';

// 'a', 'shift+z', 'mod+enter', 'space', '[', '?', 'arrowleft'. Shift only
// shows up in the name for letters and named keys: '?' and '<' already say it.
export function keyName(e) {
  const k = e.key === ' ' ? 'space' : e.key.toLowerCase();
  const named = k.length > 1 || /[a-z]/.test(k);
  return `${e.metaKey || e.ctrlKey ? 'mod+' : ''}${e.shiftKey && named ? 'shift+' : ''}${k}`;
}

export function isTyping(target) {
  return !!target?.closest?.(TYPING);
}

/**
 * map: { [keyName]: (event) => void | false }. A handler that returns false
 * leaves the default action alone. `active` false parks the listener (a help
 * dialog on top owns the keyboard).
 */
export function useShortcuts(map, active = true) {
  const ref = useRef(map);
  ref.current = map;
  useEffect(() => {
    if (!active) return undefined;
    const onKey = (e) => {
      if (e.altKey || e.isComposing) return;
      const name = keyName(e);
      const fn = ref.current[name];
      if (!fn) return;
      if (!ALWAYS.has(name) && isTyping(e.target)) return;
      if (name === 'space' && e.target?.closest?.(OWN_SPACE)) return;
      if (fn(e) !== false) e.preventDefault();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [active]);
}

/**
 * Dialog behaviour for a full-screen editor: focus moves in, Tab wraps, and
 * focus goes back to whatever opened it. `ready` re-focuses once a loading
 * state is replaced by the real panel (a new element, so focus was lost).
 */
export function useDialog(boxRef, active = true, ready = true) {
  // Captured during render, before any child can take focus.
  const opener = useRef(null);
  if (!opener.current && typeof document !== 'undefined') opener.current = document.activeElement;
  useEffect(() => {
    const box = boxRef.current;
    if (box && !box.contains(document.activeElement)) box.focus();
  }, [boxRef, ready]);
  useEffect(() => {
    const back = opener.current;
    return () => {
      if (back instanceof HTMLElement && back !== document.body && document.contains(back)) back.focus();
    };
  }, []);
  useEffect(() => {
    if (!active) return undefined;
    const onKey = (e) => trapTab(e, boxRef.current);
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [boxRef, active]);
}

// Documented in ShortcutHelp; `scope` picks the rows each editor shows.
export const SHORTCUTS = [
  { keys: ['Space'], label: 'play / pause', scope: 'clip' },
  { keys: ['J', 'K', 'L'], label: 'back 5 s / pause / forward 5 s', scope: 'clip' },
  { keys: [',', '.'], label: 'step one frame back / forward', scope: 'clip' },
  { keys: ['[', ']'], label: 'set in / set out at the playhead', scope: 'clip' },
  { keys: ['S'], label: 'split the selected segment at the playhead', scope: 'clip' },
  { keys: ['<', '>'], label: 'insert / replace the marked range', scope: 'clip' },
  { keys: ['Delete'], label: 'delete the selected segment', scope: 'clip' },
  { keys: ['S'], label: 'split the focused scene in two regions', scope: 'reframe' },
  { keys: ['Space'], label: 'play / pause the focused scene', scope: 'reframe' },
  { keys: ['←', '→'], label: 'nudge the crop 1% (Shift: 5%); on an in/out handle, 0.1 s (Shift: 1 s)', scope: 'both' },
  { keys: ['Z', 'Shift Z'], label: 'undo / redo', scope: 'both' },
  { keys: ['Ctrl/⌘ Enter'], label: 'apply', scope: 'both' },
  { keys: ['?'], label: 'show this list', scope: 'both' },
  { keys: ['Esc'], label: 'close', scope: 'both' },
];
