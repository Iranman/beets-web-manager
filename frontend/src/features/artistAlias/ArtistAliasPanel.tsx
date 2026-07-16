import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import LinearProgress from '@mui/material/LinearProgress';
import TextField from '@mui/material/TextField';
import { useCallback, useEffect, useState } from 'react';
import { confirmArtistAlias, getArtistIdGroups, mergeArtistId, rejectArtistIdGroup } from '../../api/client';
import { CleanEmptyState, CleanPanelHeader, CleanSection } from '../../components/CleanPanel';
import { useJobPoll } from '../../lib/hooks';
import type { ArtistIdGroup } from '../../api/types';

function cx(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(' ');
}

function GroupRow({
  group,
  selectedCanonical,
  busy,
  rejectBusy,
  jobStatus,
  jobLog,
  onSelect,
  onMerge,
  onReject,
}: {
  group: ArtistIdGroup;
  selectedCanonical: string;
  busy: boolean;
  rejectBusy: boolean;
  jobStatus?: string;
  jobLog?: string[];
  onSelect: (name: string) => void;
  onMerge: () => void;
  onReject: () => void;
}) {
  const lastLine = jobLog?.filter(Boolean).slice(-1)[0] ?? '';
  return (
    <div className="space-y-2 rounded-md border border-graphite-800/90 bg-graphite-950/60 p-3 shadow-sm shadow-black/20">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs text-amber-400">{group.mb_artistid}</span>
        <a
          className="text-xs text-sky-400 hover:underline"
          href={`https://musicbrainz.org/artist/${group.mb_artistid}`}
          rel="noreferrer"
          target="_blank"
        >
          MusicBrainz
        </a>
        <Chip label={`${group.album_count} albums`} size="small" variant="outlined" />
      </div>

      <div className="space-y-1">
        {group.names.map((variant) => (
          <label
            key={variant.name}
            className={cx(
              'flex cursor-pointer items-start gap-2 rounded border p-2 text-sm transition',
              selectedCanonical === variant.name
                ? 'border-red-400 bg-red-400/10 shadow-sm shadow-black/30'
                : 'border-graphite-800/90 hover:border-graphite-700 hover:bg-graphite-900/50',
            )}
          >
            <input
              checked={selectedCanonical === variant.name}
              className="mt-0.5 shrink-0 accent-red-500"
              name={`canonical-${group.mb_artistid}`}
              type="radio"
              value={variant.name}
              onChange={() => onSelect(variant.name)}
            />
            <span className="min-w-0">
              <span className="font-medium text-zinc-100">{variant.name}</span>
              <span className="ml-2 text-xs text-zinc-500">
                {variant.album_count} albums · {variant.track_count} tracks
              </span>
              {variant.credits.length > 0 && (
                <span className="ml-2 text-xs text-zinc-600">
                  +{variant.credits.reduce((s, c) => s + c.count, 0)} credits
                </span>
              )}
            </span>
          </label>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-2 pt-1">
        <Button
          disabled={busy}
          size="small"
          variant="outlined"
          onClick={onMerge}
        >
          {busy && jobStatus === 'running' ? 'Merging...' : 'Merge into selected'}
        </Button>
        <Button
          disabled={busy || rejectBusy}
          size="small"
          variant="outlined"
          onClick={onReject}
        >
          {rejectBusy ? 'Rejecting...' : 'Reject'}
        </Button>
        {jobStatus === 'running' && <Chip color="warning" label="Running" size="small" variant="outlined" />}
        {jobStatus === 'success' && <Chip color="success" label="Done" size="small" variant="outlined" />}
        {(jobStatus === 'failed' || jobStatus === 'killed') && <Chip color="error" label="Failed" size="small" variant="outlined" />}
        {jobStatus === 'running' && lastLine ? (
          <span className="max-w-xs truncate text-xs text-zinc-500">{lastLine}</span>
        ) : null}
      </div>
    </div>
  );
}

interface ArtistAliasPanelProps {
  active?: boolean;
  autoLoad?: boolean;
  onRefreshLibrary?: () => void;
}

export function ArtistAliasPanel({ active = true, autoLoad = true, onRefreshLibrary }: ArtistAliasPanelProps) {
  const [groups, setGroups] = useState<ArtistIdGroup[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [loaded, setLoaded] = useState(false);
  const [hiddenCount, setHiddenCount] = useState(0);

  const [selectedCanonicals, setSelectedCanonicals] = useState<Record<string, string>>({});
  const [activeMbid, setActiveMbid] = useState<string | null>(null);
  const [mergeJobId, setMergeJobId] = useState<string | null>(null);
  const [mergeError, setMergeError] = useState('');
  const [rejectingKey, setRejectingKey] = useState<string | null>(null);
  const [rejectError, setRejectError] = useState('');
  const [mergeAllBusy, setMergeAllBusy] = useState(false);
  const [mergeAllMsg, setMergeAllMsg] = useState('');

  const [sourceArtist, setSourceArtist] = useState('');
  const [canonicalArtist, setCanonicalArtist] = useState('');
  const [aliasMbid, setAliasMbid] = useState('');
  const [confirmJobId, setConfirmJobId] = useState<string | null>(null);
  const [confirmError, setConfirmError] = useState('');

  const { job: mergeJob } = useJobPoll(mergeJobId);
  const { job: confirmJob } = useJobPoll(confirmJobId);

  const loadGroups = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const { groups: g, hidden_count } = await getArtistIdGroups();
      setGroups(g);
      setHiddenCount(Math.max(0, Number(hidden_count ?? 0)));
      setLoaded(true);
      setSelectedCanonicals((prev) => {
        const next = { ...prev };
        for (const grp of g) {
          if (!next[grp.mb_artistid]) next[grp.mb_artistid] = grp.canonical;
        }
        return next;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!autoLoad || !active || loaded || loading) return;
    void loadGroups();
  }, [active, autoLoad, loaded, loading, loadGroups]);

  useEffect(() => {
    if (mergeJob?.status === 'success') {
      setActiveMbid(null);
      void loadGroups();
      onRefreshLibrary?.();
    }
    if (mergeJob?.status === 'failed' || mergeJob?.status === 'killed') {
      setActiveMbid(null);
    }
  }, [loadGroups, mergeJob?.status, onRefreshLibrary]);

  useEffect(() => {
    if (confirmJob?.status === 'success') {
      setSourceArtist('');
      setCanonicalArtist('');
      setAliasMbid('');
      void loadGroups();
      onRefreshLibrary?.();
    }
  }, [confirmJob?.status, loadGroups, onRefreshLibrary]);

  async function handleMerge(group: ArtistIdGroup) {
    const canonical = selectedCanonicals[group.mb_artistid] ?? group.canonical;
    setMergeError('');
    setMergeJobId(null);
    setActiveMbid(group.mb_artistid);
    try {
      const { job_id } = await mergeArtistId(group.mb_artistid, canonical);
      setMergeJobId(job_id);
    } catch (err) {
      setActiveMbid(null);
      setMergeError(err instanceof Error ? err.message : String(err));
    }
  }

  async function handleReject(group: ArtistIdGroup) {
    const key = group.reject_key || group.mb_artistid;
    setRejectError('');
    setRejectingKey(key);
    try {
      await rejectArtistIdGroup(group.mb_artistid, group.reject_key);
      setGroups((current) => current.filter((item) => (item.reject_key || item.mb_artistid) !== key));
      setHiddenCount((count) => count + 1);
    } catch (err) {
      setRejectError(err instanceof Error ? err.message : String(err));
    } finally {
      setRejectingKey(null);
    }
  }

  async function handleConfirm() {
    if (!sourceArtist.trim() || !canonicalArtist.trim()) return;
    setConfirmError('');
    setConfirmJobId(null);
    try {
      const { job_id } = await confirmArtistAlias(
        sourceArtist.trim(),
        canonicalArtist.trim(),
        aliasMbid.trim() || undefined,
      );
      setConfirmJobId(job_id);
    } catch (err) {
      setConfirmError(err instanceof Error ? err.message : String(err));
    }
  }

  async function handleMergeAll() {
    if (!groups.length) return;
    if (!window.confirm(
      `Merge all ${groups.length} alias group(s) into their selected canonical name?\n\nEach merge runs beet write + move for affected albums. Jobs will appear in the Jobs panel.`
    )) return;
    setMergeAllBusy(true);
    setMergeAllMsg('');
    let started = 0;
    let failed = 0;
    for (const group of groups) {
      const canonical = selectedCanonicals[group.mb_artistid] ?? group.canonical;
      try {
        await mergeArtistId(group.mb_artistid, canonical);
        started += 1;
      } catch {
        failed += 1;
      }
    }
    setMergeAllMsg(
      failed
        ? `Started ${started} merge job(s), ${failed} failed to start — check Jobs.`
        : `Started ${started} merge job(s). Watch progress in Jobs.`
    );
    setMergeAllBusy(false);
  }

  const mergeBusy = activeMbid !== null;

  return (
    <div className="space-y-3">
      <CleanPanelHeader
        title="Artist Alias Review"
        description="Artists sharing a MusicBrainz ID under different names can be merged into one canonical form."
        meta={loaded ? (
          <>
            <span>{groups.length} open alias group(s)</span>
            <span>{hiddenCount} rejected suggestion(s)</span>
          </>
        ) : <span>No alias groups loaded</span>}
        actions={(
          <>
          {hiddenCount > 0 ? <Chip label={`${hiddenCount} rejected`} size="small" variant="outlined" /> : null}
          {groups.length > 1 && (
            <Button
              color="warning"
              disabled={loading || mergeAllBusy || mergeBusy}
              size="small"
              variant="outlined"
              onClick={() => void handleMergeAll()}
            >
              {mergeAllBusy ? 'Starting…' : `Merge all (${groups.length})`}
            </Button>
          )}
          <Button disabled={loading} size="small" variant="outlined" onClick={() => void loadGroups()}>
            {loaded ? 'Refresh' : 'Load'}
          </Button>
          </>
        )}
      />

      {loading && <LinearProgress />}
      {error && <Alert severity="error">{error}</Alert>}
      {mergeError && <Alert severity="error">{mergeError}</Alert>}
      {rejectError && <Alert severity="error">{rejectError}</Alert>}
      {mergeAllMsg && <Alert severity="info" onClose={() => setMergeAllMsg('')}>{mergeAllMsg}</Alert>}

      {loaded && groups.length === 0 && !loading && (
        <CleanEmptyState title="No alias groups found" message="All reviewed artist names are unique per MusicBrainz ID." tone="success" />
      )}

      {groups.map((group) => (
        <GroupRow
          key={group.mb_artistid}
          busy={mergeBusy}
          group={group}
          jobLog={activeMbid === group.mb_artistid ? mergeJob?.log : undefined}
          jobStatus={activeMbid === group.mb_artistid ? mergeJob?.status : undefined}
          rejectBusy={rejectingKey === (group.reject_key || group.mb_artistid)}
          selectedCanonical={selectedCanonicals[group.mb_artistid] ?? group.canonical}
          onMerge={() => void handleMerge(group)}
          onReject={() => void handleReject(group)}
          onSelect={(name) => setSelectedCanonicals((prev) => ({ ...prev, [group.mb_artistid]: name }))}
        />
      ))}

      {/* Manual alias confirmation */}
      <CleanSection
        title="Manual Alias Confirmation"
        description="Use this when the automatic alias review does not surface a known artist rename."
      >
        <div className="grid gap-2 sm:grid-cols-3">
          <TextField
            label="Source artist"
            placeholder="Kanye West"
            size="small"
            value={sourceArtist}
            onChange={(e) => setSourceArtist(e.target.value)}
          />
          <TextField
            label="Canonical artist"
            placeholder="Ye"
            size="small"
            value={canonicalArtist}
            onChange={(e) => setCanonicalArtist(e.target.value)}
          />
          <TextField
            label="MB artist ID (optional)"
            placeholder="164f0d73-1234-..."
            size="small"
            value={aliasMbid}
            onChange={(e) => setAliasMbid(e.target.value)}
          />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            disabled={!sourceArtist.trim() || !canonicalArtist.trim() || confirmJob?.status === 'running'}
            size="small"
            variant="outlined"
            onClick={() => void handleConfirm()}
          >
            {confirmJob?.status === 'running' ? 'Running...' : 'Confirm'}
          </Button>
          {confirmJob?.status === 'running' && <Chip color="warning" label="Running" size="small" variant="outlined" />}
          {confirmJob?.status === 'success' && <Chip color="success" label="Done" size="small" variant="outlined" />}
          {(confirmJob?.status === 'failed' || confirmJob?.status === 'killed') && (
            <Chip color="error" label="Failed" size="small" variant="outlined" />
          )}
          {confirmJob?.status === 'running' && (
            <span className="max-w-xs truncate text-xs text-zinc-500">
              {confirmJob.log?.filter(Boolean).slice(-1)[0] ?? ''}
            </span>
          )}
          {confirmError && <span className="text-xs text-red-400">{confirmError}</span>}
        </div>
      </CleanSection>
    </div>
  );
}
