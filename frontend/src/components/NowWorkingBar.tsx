import { useEffect, useMemo, useRef, useState } from 'react';
import { NavLink } from 'react-router';
import { useGlobalJobs } from '../lib/hooks';
import { buildJobFeed, cleanJobLogLine, friendlyJobTitle, isTechnicalLogNoise } from '../lib/jobFeed';
import type { Job } from '../types/api';
import { RecordRing } from './RecordRing';
import { SleeveArt, albumArtSources } from './SleeveArt';

// The "now working" station: a persistent, compact bar docked at the foot of the shell showing the
// one running job, built entirely from job state the frontend already holds (the shared ['jobs']
// query via useGlobalJobs). It is presentational only — it never starts, cancels or changes a job.
// Idle: it renders nothing. On completion it shows a short, quiet confirmation (a brass check and a
// state word), announced politely so it never relies on motion or colour alone.

// ponytail: mirrors Jobs.tsx jobArtSources; keep the metadata keys in sync if that one changes.
function jobArt(job: Job): string[] {
  const m = job.metadata;
  if (!m) return [];
  return albumArtSources({
    albumId: (m.existing_album_id ?? m.album_id) as number | string | undefined,
    releaseGroupId: typeof m.mb_releasegroupid === 'string' ? m.mb_releasegroupid : null,
  });
}

function jobPercent(job: Job): number | null {
  const raw = job.state?.progress_percent;
  return typeof raw === 'number' && Number.isFinite(raw) ? Math.max(0, Math.min(100, raw)) : null;
}

interface Finished {
  title: string;
  ok: boolean;
}

/** A brass ring that has "closed" to a check (done) or an alert (finished with errors). Decorative;
 *  the state word beside it carries the meaning. */
function OutcomeMark({ ok }: { ok: boolean }) {
  return (
    <svg aria-hidden="true" className="size-6 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="10" className="text-brass-500" strokeWidth="1.5" />
      {ok ? (
        <path d="M8 12.5l2.5 2.5L16 9" className="text-brass-200" />
      ) : (
        <>
          <path d="M12 8v4.5" className="text-brass-200" />
          <path d="M12 16h.01" className="text-brass-200" />
        </>
      )}
    </svg>
  );
}

export default function NowWorkingBar() {
  const { jobs } = useGlobalJobs();
  const list = jobs ?? [];
  const running = list.find((j) => j.status === 'running') ?? null;

  const [expanded, setExpanded] = useState(false);
  const [done, setDone] = useState<Finished | null>(null);
  const prevRunningId = useRef<string | null>(null);

  // Detect the running job finishing: when the primary running id clears, look that job up in the
  // same list to read its final status and show a brief confirmation. A new running job clears it.
  useEffect(() => {
    const cur = running?.job_id ?? null;
    if (cur === prevRunningId.current) return;
    if (!cur && prevRunningId.current) {
      const finished = list.find((j) => j.job_id === prevRunningId.current);
      if (finished && finished.status !== 'running' && finished.status !== 'missing') {
        setDone({ title: friendlyJobTitle(finished), ok: finished.status === 'success' });
      }
    }
    if (cur) {
      setDone(null);
      setExpanded(false);
    }
    prevRunningId.current = cur;
  }, [running, list]);

  // The confirmation is quiet: it clears itself after a few seconds.
  useEffect(() => {
    if (!done) return;
    const t = setTimeout(() => setDone(null), 5000);
    return () => clearTimeout(t);
  }, [done]);

  const model = useMemo(() => (running ? buildJobFeed(running, running.log ?? []) : null), [running]);
  const logLines = useMemo(() => {
    if (!running) return [];
    return (running.log ?? [])
      .map(cleanJobLogLine)
      .filter((line) => line && !isTechnicalLogNoise(line))
      .slice(-8);
  }, [running]);

  if (running && model) {
    const pct = jobPercent(running);
    const art = jobArt(running);
    const { title } = model.summary;
    const step = model.summary.currentPhase;
    const detail = model.summary.progressText;
    return (
      <>
        {/* Spacer so the fixed bar never hides the end of the page's content. */}
        <div aria-hidden="true" className="h-14 shrink-0" />
        <div className="nowbar fixed inset-x-0 bottom-0 z-30" data-testid="now-working-bar">
          {expanded && (
            <div className="mx-auto max-w-screen-2xl px-3 pt-2 sm:px-4">
              <div className="max-h-40 overflow-y-auto rounded-sm border border-walnut-700 bg-walnut-950 p-2">
                {logLines.length > 0 ? (
                  <ul className="m-0 list-none space-y-0.5 p-0 font-mono text-xs leading-relaxed text-zinc-300">
                    {logLines.map((line, i) => (
                      <li key={i} className="truncate">{line}</li>
                    ))}
                  </ul>
                ) : (
                  <p className="m-0 text-xs text-muted">
                    No live log lines yet.{' '}
                    <NavLink to="/jobs" className="text-brass-300 underline hover:text-brass-200">Open Jobs &amp; Cleanup</NavLink> for full detail.
                  </p>
                )}
              </div>
            </div>
          )}
          <div className="nowbar-progress" data-indeterminate={pct === null ? 'true' : 'false'}>
            <div className="nowbar-progress-fill" style={pct !== null ? { width: `${pct}%` } : undefined} />
          </div>
          <div className="mx-auto flex h-14 max-w-screen-2xl items-center gap-2 px-3 sm:gap-3 sm:px-4" aria-label="Now working">
            <RecordRing className="size-6 shrink-0" spinning />
            <span className="hidden shrink-0 sm:inline-flex">
              {art.length > 0 ? <SleeveArt size={36} sources={art} /> : <RecordRing className="size-9" />}
            </span>
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm font-medium text-zinc-100">{title}</div>
              <div className="flex min-w-0 items-center gap-2 text-xs text-muted">
                <span className="liner-label shrink-0">{step}</span>
                {detail ? <span className="truncate tabular-nums">{detail}</span> : null}
              </div>
            </div>
            {pct !== null && (
              <span className="shrink-0 text-xs font-semibold tabular-nums text-brass-200">{Math.round(pct)}%</span>
            )}
            <button
              type="button"
              className="nowbar-btn flex shrink-0 items-center gap-1 rounded-sm px-2 text-xs font-medium text-zinc-300 hover:bg-walnut-800 hover:text-zinc-50 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-brass-300"
              aria-expanded={expanded}
              onClick={() => setExpanded((v) => !v)}
            >
              <svg aria-hidden="true" className={`size-4 transition-transform duration-150 ${expanded ? 'rotate-180' : ''}`} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M18 15l-6-6-6 6" />
              </svg>
              <span className="hidden sm:inline">{expanded ? 'Hide log' : 'Details'}</span>
            </button>
            <NavLink
              to="/jobs"
              className="nowbar-btn flex shrink-0 items-center rounded-sm px-2 text-xs font-medium text-brass-300 hover:bg-walnut-800 hover:text-brass-200 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-brass-300"
            >
              Jobs
            </NavLink>
          </div>
          <span className="sr-only" aria-live="off">
            Working: {title}. {step}. {detail}{pct !== null ? ` ${Math.round(pct)}%` : ''}
          </span>
        </div>
      </>
    );
  }

  if (done) {
    return (
      <>
        <div aria-hidden="true" className="h-14 shrink-0" />
        <div className="nowbar fixed inset-x-0 bottom-0 z-30" data-testid="now-working-bar" role="status">
          <div className="mx-auto flex h-14 max-w-screen-2xl items-center gap-3 px-3 sm:px-4">
            <OutcomeMark ok={done.ok} />
            <div className="min-w-0 flex-1 truncate text-sm">
              <span className={`font-semibold ${done.ok ? 'text-success' : 'text-danger-text'}`}>
                {done.ok ? 'Completed' : 'Finished with errors'}
              </span>
              <span className="ml-2 text-zinc-200">{done.title}</span>
            </div>
            <NavLink
              to="/jobs"
              className="nowbar-btn flex shrink-0 items-center rounded-sm px-2 text-xs font-medium text-brass-300 hover:bg-walnut-800 hover:text-brass-200 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-brass-300"
            >
              View in Jobs
            </NavLink>
          </div>
        </div>
      </>
    );
  }

  return null;
}
