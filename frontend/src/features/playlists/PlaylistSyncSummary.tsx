import { formatShortTime } from '../../lib/dates';
import Alert from '@mui/material/Alert';
import type { PlaylistSyncDetail, PlaylistSyncStatusResponse } from '../../api/types';

const fmt = new Intl.NumberFormat();

/** One short line: auto-sync mode, last and next run, and the last totals. */
export function playlistSyncStatusLabel(status: PlaylistSyncStatusResponse | null): string {
  if (!status) return '';
  const parts = [status.enabled ? `Auto every ${Math.round((status.interval || 0) / 60)} min` : 'Auto off'];
  if (status.running) parts.push('Syncing now');
  parts.push(`Last ${status.last_run ? formatShortTime(status.last_run) : 'never'}`);
  if (status.enabled && status.next_run_at && !status.running) parts.push(`Next ${formatShortTime(status.next_run_at)}`);
  const r = status.last_result;
  if (r) {
    parts.push(`${fmt.format(r.playlists_updated ?? 0)} updated`);
    if (r.playlists_failed) parts.push(`${fmt.format(r.playlists_failed)} failed`);
    if (r.playlists_skipped_busy) parts.push(`${fmt.format(r.playlists_skipped_busy)} skipped (busy)`);
    parts.push(`${fmt.format(r.plex_writes ?? 0)} Plex write${r.plex_writes === 1 ? '' : 's'}`);
    if (r.pending) parts.push(`${fmt.format(r.pending)} pending`);
  }
  return parts.join(' · ');
}

function detailProblem(detail: PlaylistSyncDetail): { severity: 'error' | 'warning' | 'info'; text: string } | null {
  if (detail.error) {
    return { severity: 'error', text: `Sync failed${detail.error_type ? ` (${detail.error_type})` : ''}. The saved list was not changed; see the sync job log.` };
  }
  if (detail.plex_error) return { severity: 'error', text: `Plex step failed: ${detail.plex_error}` };
  if (detail.skipped_reason === 'pipeline_running') {
    return { severity: 'info', text: 'Skipped: a download or import job for this playlist was running. It syncs on the next run.' };
  }
  if (detail.plex_note) return { severity: 'warning', text: detail.plex_note };
  if (detail.pending_count) {
    return { severity: 'warning', text: `${fmt.format(detail.pending_count)} track${detail.pending_count === 1 ? '' : 's'} pending.` };
  }
  return null;
}

/** Last sync outcome: legacy-import notice and the playlists that need a look
 * (failed, skipped, or with pending tracks and their reasons). */
export function PlaylistSyncSummary({ status }: { status: PlaylistSyncStatusResponse | null }) {
  const result = status?.last_result;
  if (!result) return null;
  const legacy = Array.isArray(result.legacy_imported) ? result.legacy_imported : [];
  const rows = (result.details ?? [])
    .map((detail) => ({ detail, problem: detailProblem(detail) }))
    .filter((row) => row.problem);
  if (!legacy.length && !rows.length) return null;
  return (
    <div className="mb-3 space-y-2" data-testid="playlist-sync-summary">
      {legacy.length ? (
        <Alert severity="info">
          Imported {legacy.length} playlist{legacy.length === 1 ? '' : 's'} from the older playlists folder ({legacy.join(', ')}). The old files were only read and were not changed.
        </Alert>
      ) : null}
      {rows.length ? (
        <details className="rounded border border-graphite-800 bg-graphite-950 px-3 py-2 text-xs">
          <summary className="cursor-pointer text-zinc-300">
            Last sync: {rows.length} playlist{rows.length === 1 ? '' : 's'} need a look
          </summary>
          <ul className="mt-2 space-y-2">
            {rows.map(({ detail, problem }) => (
              <li key={detail.name}>
                <div className={problem!.severity === 'error' ? 'text-red-300' : problem!.severity === 'warning' ? 'text-amber-300' : 'text-zinc-300'}>
                  <span className="font-medium">{detail.name}</span>: {problem!.text}
                  {detail.plex_mode === 'created' ? ' Plex playlist was created new.' : ''}
                </div>
                {detail.pending?.length ? (
                  <ul className="ml-4 mt-1 list-disc text-zinc-400">
                    {detail.pending.map((track, index) => (
                      <li key={`${track.artist}-${track.title}-${index}`}>
                        {track.artist ? `${track.artist} - ` : ''}{track.title}: {track.reason}
                      </li>
                    ))}
                    {(detail.pending_count ?? 0) > detail.pending.length ? (
                      <li>and {fmt.format((detail.pending_count ?? 0) - detail.pending.length)} more</li>
                    ) : null}
                  </ul>
                ) : null}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  );
}
