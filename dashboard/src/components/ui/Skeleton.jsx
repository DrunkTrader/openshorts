// Loading placeholder; the shimmer is switched off by the global reduced-motion rule.
export default function Skeleton({ className = '', ...props }) {
  return <div aria-hidden="true" className={`skeleton rounded-sm ${className}`} {...props} />;
}
