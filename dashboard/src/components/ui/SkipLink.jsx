// "Skip to main content" for pages whose route lives in the hash (a #anchor
// link would change the route), so it moves focus instead of navigating.
export default function SkipLink({ label = 'Skip to main content' }) {
  return (
    <button
      type="button"
      onClick={() => { const m = document.querySelector('main'); if (m) { m.focus(); m.scrollIntoView(); } }}
      className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-[100] btn-primary px-4 py-2 text-xs"
    >
      {label}
    </button>
  );
}
