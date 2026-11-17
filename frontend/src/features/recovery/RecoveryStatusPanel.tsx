import Alert from '@mui/material/Alert';
import LinearProgress from '@mui/material/LinearProgress';
import { useCallback, useEffect, useState } from 'react';
import { getRecoveryAdvanceStatus } from '../../api/client';
import type { RecoveryAdvanceCounts, RecoveryAdvanceStatus } from '../../api/types';
import { CleanEmptyState, CleanMetricGrid, CleanPanelHeader } from '../../components/CleanPanel';
import { normaliseEpochSeconds } from '../../lib/dates';
import { errorMessage } from '../../lib/errors';
import { useInterval } from '../../lib/hooks';

const numberFmt = new Intl.NumberFormat();

// The sidecar refreshes server-side roughly every 120s; a 30s read keeps the
// passive panel current without being chatty.
const POLL_MS = 30_000;

function count(counts: RecoveryAdvanceCounts | undefined, ...keys: Array<keyof RecoveryAdvanceCounts>): number {
  if (!counts) return 0;
  for (const key of keys) {
    const value = counts[key];
    if (typeof value === 'number' && Number.isFinite(value)) return value;
  }
  return 0;
}

function timeAgo(ts?: number | null): string {
  const seconds = normaliseEpochSeconds(ts);
  if (seconds === null) return 'unknown';
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - seconds));
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

function timeUntil(ts?: number | null): string {
  const seconds = normaliseEpochSeconds(ts);
  if (seconds === null) return '';
  const secs = Math.floor(seconds - Date.now() / 1000);
  if (secs <= 0) return 'due now';
  if (secs < 60) return `in ${secs}s`;
  if (secs < 3600) return `in ${Math.floor(secs / 60)}m`;
  return `in ${Math.floor(secs / 3600)}h`;
}

/** Passive, read-only status of the missing-track recovery advancer (#762).
 * The scheduled sweep no longer records a Job when nothing changes, so this is
 * where routine progress is visible. Browse-only: no actions. */
export function RecoveryStatusPanel() {
  const [status, setStatus] = useState<RecoveryAdvanceStatus | null>(null);
  const [hasRun, setHasRun] = useState(false);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState('');

  const load = useCallback(async () => {
    try {
      const res = await getRecoveryAdvanceStatus();
      setStatus(res.status ?? {});
      setHasRun(Boolean(res.has_run));
      setFailed('');
    } catch (err) {
      setFailed(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);
  useInterval(load, POLL_MS);

  const counts = status?.counts;
  const examined = count(counts, 'examined', 'scanned');
  const recovered = count(counts, 'recovered', 'handed_off');
  const unresolved = count(counts, 'still_unresolved', 'kept_review');
  const skipped = count(counts, 'skipped');
  const newlyReview = count(counts, 'newly_review');
  const error = (status?.error ?? '').trim();
  const changesMade = Boolean(status?.changes_made);
  const needsAttention = Boolean(status?.needs_attention) || Boolean(error) || newlyReview > 0;

  const scheduled = status?.next_scheduled_check;
  const nextCheck = scheduled == null
    ? 'Automatic checks are off.'
    : `Next automatic check ${timeUntil(scheduled)}.`;

  const description = (
    <>
      Routine background sweeps that advance stuck missing-track recoveries. They
      run quietly and only record a Job when something changes or needs attention.
    </>
  );

  return (
    <section aria-label="Missing-track recovery status">
      <CleanPanelHeader
        title="Missing-track recovery"
        description={description}
        meta={
          !loading && !failed && hasRun ? (
            <>
              <span className="liner-label">Last run</span>
              <span className="text-zinc-300">{timeAgo(status?.last_run)}</span>
              {status?.trigger ? (
                <span className="text-muted">
                  ({status.trigger === 'scheduled' ? 'background sweep' : 'manual run'})
                </span>
              ) : null}
              <span className="text-muted">·</span>
              <span className="text-muted">{nextCheck}</span>
            </>
          ) : null
        }
      >
        {loading ? (
          <LinearProgress sx={{ borderRadius: 1 }} />
        ) : failed ? (
          <Alert severity="error">
            The recovery status could not be loaded, so it is unknown right now.
            It will refresh on its own, or reload the page to try again. ({failed})
          </Alert>
        ) : !hasRun ? (
          <CleanEmptyState
            title="No recovery sweep has run yet"
            message="Once the background check runs, its results appear here."
          />
        ) : (
          <div className="space-y-3">
            <CleanMetricGrid
              items={[
                { label: 'Examined', value: numberFmt.format(examined) },
                { label: 'Recovered', value: numberFmt.format(recovered), tone: recovered > 0 ? 'success' : 'neutral' },
                { label: 'Still unresolved', value: numberFmt.format(unresolved), tone: unresolved > 0 ? 'warning' : 'neutral' },
                { label: 'Skipped', value: numberFmt.format(skipped) },
              ]}
            />
            {needsAttention ? (
              <CleanEmptyState
                tone={error ? 'danger' : 'warning'}
                title={error ? 'The last sweep reported an error' : `${numberFmt.format(newlyReview)} newly need review`}
                message={
                  error
                    ? `The recovery sweep failed with ${error}. It will retry on the next scheduled check; open the Review Queue if items keep piling up.`
                    : 'New stuck recoveries were moved to the Review Queue for a decision. Open Import > Review Queue to resolve them.'
                }
              />
            ) : changesMade ? (
              <p className="text-sm text-success">
                Recovered {numberFmt.format(recovered)} missing track{recovered === 1 ? '' : 's'} on the last sweep.
              </p>
            ) : (
              <p className="text-sm text-muted">No missing tracks required recovery. No changes made.</p>
            )}
          </div>
        )}
      </CleanPanelHeader>
    </section>
  );
}
