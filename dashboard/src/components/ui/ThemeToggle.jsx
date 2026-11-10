import { useEffect, useState } from 'react';
import { Moon, Sun } from 'lucide-react';
import { currentTheme, setTheme, watchSystemTheme } from '../../lib/theme';

// Light/dark switch. Starts from the pre-paint value, follows the OS until clicked.
export default function ThemeToggle({ className = '' }) {
  const [theme, setLocal] = useState(() => document.documentElement.dataset.theme || currentTheme());
  useEffect(() => {
    const stop = watchSystemTheme();
    const obs = new MutationObserver(() => setLocal(document.documentElement.dataset.theme));
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    return () => { stop(); obs.disconnect(); };
  }, []);
  const dark = theme === 'dark';
  return (
    <button
      type="button"
      onClick={() => setTheme(dark ? 'light' : 'dark')}
      aria-label={dark ? 'Switch to light theme' : 'Switch to dark theme'}
      title={dark ? 'Light theme' : 'Dark theme'}
      className={`w-8 h-8 rounded-full flex items-center justify-center text-muted hover:text-ink hover:bg-paper3 transition-colors ${className}`}
    >
      {dark ? <Sun size={16} /> : <Moon size={16} />}
    </button>
  );
}
