// Raised surface: large radius, hairline, soft shadow. `hover` lifts it on pointer-over.
export default function Card({ as: Tag = 'div', hover = false, className = '', ...props }) {
  return <Tag className={`card ${hover ? 'card-hover' : ''} ${className}`} {...props} />;
}
