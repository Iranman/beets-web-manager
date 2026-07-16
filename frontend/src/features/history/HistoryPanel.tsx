import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import LinearProgress from '@mui/material/LinearProgress';
import { useCallback, useEffect, useState } from 'react';
import { clearRecentImports, getAiMatchHistory, getRecentImports } from '../../api/client';
import type { RecentImport } from '../../api/types';

const CONF_COLOR = {
  high:   'success',
  medium: 'warning',
  low:    'error',
} as const;

function normaliseEpochSeconds(ts?: number | string | null) {
  const value = Number(ts);
  if (!Number.isFinite(value) || value <= 0) return null;
  return value > 100000000000 ? value / 1000 : value;
}

function entryTimestamp(entry: RecentImport) {
  return entry.matched_at ?? entry.imported_at;
}

function timeAgo(ts?: number | string | null): string {
  const seconds = normaliseEpochSeconds(ts);
  if (seconds === null) return '';
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - seconds));
  if (secs < 60)     return `${secs}s ago`;
  if (secs < 3600)   return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400)  return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

function HistoryRow({ entry }: { entry: RecentImport }) {
  const conf = (entry.confidence ?? '').toLowerCase() as keyof typeof CONF_COLOR;
  const mbUrl = entry.mb_url || (entry.mb_albumid ? `https://musicbrainz.org/release/${entry.mb_albumid}` : '');

  return (
    <div className="grid grid-cols-[minmax(0,1fr)_auto] items-start gap-3 border-t border-graphite-800 px-3 py-3 text-sm">
      <div className="min-w-0 space-y-0.5">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="font-medium text-zinc-200">
            {entry.artist || '—'}{entry.album ? ` — ${entry.album}` : ''}
          </span>
          {entry.year ? <span className="text-zinc-500">({entry.year})</span> : null}
        </div>
        {entry.reason && (
          <div className="text-[0.72rem] italic text-zinc-500">{entry.reason}</div>
        )}
        <div className="truncate font-mono text-[0.68rem] text-zinc-600">
          {entry.original_folder || entry.original_path || entry.aldir}
        </div>
      </div>

      <div className="flex shrink-0 flex-col items-end gap-1.5">
        {conf in CONF_COLOR && (
          <Chip
            color={CONF_COLOR[conf]}
            label={conf}
            size="small"
            variant="outlined"
          />
        )}
        <span className="text-[0.7rem] tabular-nums text-zinc-600">
          {timeAgo(entryTimestamp(entry))}
        </span>
        {mbUrl && (
          <a
            className="text-[0.7rem] text-red-400 hover:text-red-300"
            href={mbUrl}
            rel="noreferrer"
            target="_blank"
          >
            MusicBrainz ↗
          </a>
        )}
      </div>
    </div>
  );
}

function HistorySection({
  title,
  entries,
  onClear,
}: {
  title: string;
  entries: RecentImport[];
  onClear?: () => void;
}) {
  return (
    <div className="rounded border border-graphite-800 bg-graphite-950">
      <div className="flex items-center justify-between border-b border-graphite-800 px-3 py-2">
        <span className="text-[0.75rem] font-semibold uppercase tracking-wide text-zinc-500">
          {title} ({entries.length})
        </span>
        {onClear && entries.length > 0 && (
          <Button color="error" size="small" variant="text" sx={{ fontSize: '0.7rem' }} onClick={onClear}>
            Clear
          </Button>
        )}
      </div>
      {entries.length === 0 ? (
        <div className="px-3 py-4 text-sm text-zinc-600">No entries yet.</div>
      ) : (
        entries.map((entry, i) => (
          <HistoryRow
            key={`${entryTimestamp(entry) ?? i}-${entry.original_path ?? entry.aldir ?? i}`}
            entry={entry}
          />
        ))
      )}
    </div>
  );
}

export function HistoryPanel() {
  const [recent, setRecent] = useState<RecentImport[]>([]);
  const [aiHistory, setAiHistory] = useState<RecentImport[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [r, h] = await Promise.all([getRecentImports(), getAiMatchHistory(50)]);
      setRecent(r.imports ?? []);
      setAiHistory(h.history ?? []);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const handleClearRecent = async () => {
    await clearRecentImports();
    void load();
  };

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-medium text-zinc-200">Recently imported</h2>
        <Button size="small" variant="outlined" onClick={load} disabled={loading}>
          Refresh
        </Button>
      </div>

      {loading && <LinearProgress sx={{ borderRadius: 1 }} />}
      {error && <Alert severity="error">{error}</Alert>}

      {!loading && (
        <div className="space-y-5">
          <HistorySection
            title="Recently imported"
            entries={recent}
            onClear={handleClearRecent}
          />
          <HistorySection
            title="AI-matched imports"
            entries={aiHistory}
          />
        </div>
      )}
    </div>
  );
}
