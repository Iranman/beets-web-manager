// The brass "record ring" signature: a vinyl centre-hole ring. One shape, reused as the empty-shelf
// mark, the loading spinner and the selected-state dot. Always decorative (hidden from assistive
// tech, per the project's inline-SVG rule): give it a visible label beside it, or an sr-only string
// where it stands alone. `spinning` turns it, but only under prefers-reduced-motion: no-preference
// (the CSS animation is motion-gated), so it never spins when the reader asked for stillness.
export function RecordRing({
  className = 'size-6',
  spinning = false,
}: {
  className?: string;
  spinning?: boolean;
}) {
  return (
    <svg
      aria-hidden="true"
      className={`record-ring${spinning ? ' is-spinning' : ''} ${className}`}
      viewBox="0 0 48 48"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
    >
      {/* Vinyl body with a few grooves. */}
      <circle cx="24" cy="24" r="22.5" className="rr-body" />
      <circle cx="24" cy="24" r="18" className="rr-groove" />
      <circle cx="24" cy="24" r="14.5" className="rr-groove" />
      {/* The one bright arc: the brass glint that sweeps as it turns. */}
      <path d="M24 2.5 A21.5 21.5 0 0 1 45.5 24" className="rr-glint" strokeLinecap="round" />
      {/* Brass centre label and the spindle hole. */}
      <circle cx="24" cy="24" r="8.5" className="rr-label" />
      <circle cx="24" cy="24" r="2" className="rr-hole" />
    </svg>
  );
}
