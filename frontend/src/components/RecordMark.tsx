// Record-disc mark: black vinyl with grooves and a brass centre label. Decorative only.
// `playing` sets the tonearm down on the grooves; the header passes it while a job runs.
export function RecordMark({ className = 'size-8', playing = false }: { className?: string; playing?: boolean }) {
  return (
    <svg aria-hidden="true" className={`shrink-0 text-brass-400 ${className}`} viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
      <circle cx="16" cy="16" r="15" className="fill-zinc-950" stroke="currentColor" strokeOpacity="0.55" />
      <circle cx="16" cy="16" r="12" className="stroke-graphite-800" strokeWidth="0.8" />
      <circle cx="16" cy="16" r="9.5" className="stroke-graphite-800" strokeWidth="0.8" />
      <path d="M6.5 11.5A11 11 0 0 1 11.5 6" className="stroke-brass-200" strokeOpacity="0.35" strokeWidth="1.2" strokeLinecap="round" />
      <circle cx="16" cy="16" r="6" fill="currentColor" />
      <circle cx="16" cy="16" r="1.4" className="fill-graphite-950" />
      {playing && (
        <g data-testid="record-tonearm" className="text-brass-200">
          <path d="M29 3.5L22.5 19.5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
          <circle cx="29" cy="3.5" r="1.8" fill="currentColor" />
        </g>
      )}
    </svg>
  );
}
