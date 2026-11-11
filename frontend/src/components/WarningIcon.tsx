/** Decorative warning triangle; the adjacent text carries the meaning (#612). */
export function WarningIcon({ className = 'inline-block size-[1em] shrink-0 align-[-0.125em]' }: { className?: string }) {
  return (
    <svg
      aria-hidden="true"
      data-icon="warning"
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" />
      <path d="M12 9v4" />
      <path d="M12 17h.01" />
    </svg>
  );
}
