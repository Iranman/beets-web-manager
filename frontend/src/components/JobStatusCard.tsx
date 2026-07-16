import LinearProgress from '@mui/material/LinearProgress';
import { NavLink } from 'react-router-dom';
import type { Job } from '../types/api';

interface Props {
  job: Job | null;
  /** Override label shown while running (defaults to job.label) */
  runningLabel?: string;
  /** Show the last N log lines while running (default 2) */
  logLines?: number;
  className?: string;
}

const STATUS_STYLES: Record<string, string> = {
  running:   'border-amber-800/45 bg-amber-950/12',
  success:   'border-emerald-900/45 bg-emerald-950/12',
  failed:    'border-red-900/55 bg-red-950/16',
  killed:    'border-red-900/55 bg-red-950/16',
  cancelled: 'border-graphite-700 bg-graphite-900/55',
};

const STATUS_DOT: Record<string, string> = {
  running:   'bg-amber-300 animate-pulse',
  success:   'bg-emerald-400',
  failed:    'bg-red-400',
  killed:    'bg-red-400',
  cancelled: 'bg-zinc-500',
};

const STATUS_TEXT: Record<string, string> = {
  running:   'text-amber-300',
  success:   'text-emerald-300',
  failed:    'text-red-300',
  killed:    'text-red-300',
  cancelled: 'text-zinc-400',
};

export function JobStatusCard({ job, runningLabel, logLines = 2, className = '' }: Props) {
  if (!job) return null;

  const isRunning = job.status === 'running';
  const label = isRunning && runningLabel ? runningLabel : (job.label ?? 'Job');
  const lastLines = (job.log ?? []).slice(-logLines).filter(Boolean);
  const containerStyle = STATUS_STYLES[job.status] ?? STATUS_STYLES.running;
  const dotStyle = STATUS_DOT[job.status] ?? STATUS_DOT.running;
  const textStyle = STATUS_TEXT[job.status] ?? STATUS_TEXT.running;

  return (
    <div className={`overflow-hidden rounded-md border ${containerStyle} ${className}`}>
      {isRunning ? (
        <LinearProgress
          variant="indeterminate"
          sx={{
            height: 3,
            borderRadius: 0,
            backgroundColor: 'rgba(245,158,11,0.12)',
            '& .MuiLinearProgress-bar': { backgroundColor: '#b91c1c' },
          }}
        />
      ) : null}

      <div className="flex items-start gap-2.5 px-3 py-2.5">
        <span className={`mt-[3px] h-2 w-2 shrink-0 rounded-full ${dotStyle}`} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-2">
            <span className="truncate text-sm font-medium text-zinc-100">{label}</span>
            <span className={`text-[0.67rem] font-semibold uppercase tracking-wide ${textStyle}`}>
              {job.status}
            </span>
            {!isRunning ? (
              <NavLink
                to="/jobs"
                className="ml-auto text-[0.68rem] text-zinc-500 underline underline-offset-2 hover:text-zinc-300"
              >
                view log
              </NavLink>
            ) : null}
          </div>

          {lastLines.length > 0 ? (
            <div className="mt-1.5 space-y-1 border-l border-graphite-700 pl-2">
              {lastLines.map((line, i) => (
                <div
                  key={`${i}-${line}`}
                  className="truncate font-mono text-[0.66rem] leading-4 text-zinc-500"
                  title={line}
                >
                  {line}
                </div>
              ))}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
