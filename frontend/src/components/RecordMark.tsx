// Record-disc mark: black vinyl with grooves and a brass centre label. Decorative only.
export function RecordMark({ className = 'size-8' }: { className?: string }) {
  return (
    <svg aria-hidden="true" className={`shrink-0 text-brass-400 ${className}`} viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
      <circle cx="16" cy="16" r="15" className="fill-zinc-950" stroke="currentColor" strokeOpacity="0.55" />
      <circle cx="16" cy="16" r="12" className="stroke-graphite-800" strokeWidth="0.8" />
      <circle cx="16" cy="16" r="9.5" className="stroke-graphite-800" strokeWidth="0.8" />
      <path d="M6.5 11.5A11 11 0 0 1 11.5 6" className="stroke-brass-200" strokeOpacity="0.35" strokeWidth="1.2" strokeLinecap="round" />
      <circle cx="16" cy="16" r="6" fill="currentColor" />
      <circle cx="16" cy="16" r="1.4" className="fill-graphite-950" />
    </svg>
  );
}
