// Thin wrapper over the .btn-* classes in index.css.
const VARIANTS = { primary: 'btn-primary', ghost: 'btn-ghost', quiet: 'btn-quiet', danger: 'btn-danger' };

export default function Button({ variant = 'primary', className = '', type = 'button', ...props }) {
  return <button type={type} className={`${VARIANTS[variant] || VARIANTS.primary} ${className}`} {...props} />;
}
