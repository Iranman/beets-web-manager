import { Dialog, DialogBackdrop, DialogPanel, DialogTitle } from '@headlessui/react';
import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import LinearProgress from '@mui/material/LinearProgress';
import TextField from '@mui/material/TextField';
import { useCallback, useEffect, useState } from 'react';
import {
  getConfigFile,
  getHealth,
  getMbidStatus,
  getMusicFormatPreferences,
  getMusicFormatReplacementStatuses,
  getStats,
  revertConfigFile,
  saveConfigFile,
  saveMusicFormatPreferences,
  startMusicFormatReplacement,
  startMusicFormatScan,
} from '../api/client';
import { PluginsPanel } from '../features/plugins/PluginsPanel';
import type {
  ConfigFileResponse,
  HealthChecks,
  HealthResponse,
  MbidStatusResponse,
  MusicFormatKey,
  MusicFormatLayout,
  MusicFormatPreferences,
  MusicFormatReplacementTrack,
  StatsResponse,
} from '../api/types';

// ── Token / integration row ───────────────────────────────────────────────────

const INTEGRATIONS: Array<{
  key: keyof HealthChecks;
  label: string;
  description: string;
}> = [
  { key: 'library_path',   label: 'Beets library',   description: '/config/musiclibrary.blb' },
  { key: 'beet_bin',       label: 'beet binary',      description: 'fpcalc / beet in PATH' },
  { key: 'music_root',     label: 'Music root',       description: '/data/media/music' },
  { key: 'openai_key',     label: 'OpenAI',           description: 'AI matching + batch import' },
  { key: 'discogs_token',  label: 'Discogs',          description: 'Artist images + discography' },
  { key: 'lidarr_key',     label: 'Lidarr',           description: 'Wanted albums + monitoring' },
  { key: 'slskd_key',      label: 'Soulseek (slskd)', description: 'Missing-track downloads' },
];

function StatusDot({ ok }: { ok: boolean }) {
  return (
    <span
      className={`inline-block size-2.5 rounded-full ${ok ? 'bg-emerald-400' : 'bg-red-500'}`}
      aria-label={ok ? 'configured' : 'not configured'}
    />
  );
}

function IntegrationRow({
  label,
  description,
  ok,
}: {
  label: string;
  description: string;
  ok: boolean;
}) {
  return (
    <div className="flex items-center gap-3 rounded border border-graphite-800 bg-graphite-900 px-4 py-3">
      <StatusDot ok={ok} />
      <div className="flex-1">
        <div className="text-[0.82rem] font-medium text-zinc-200">{label}</div>
        <div className="text-[0.72rem] text-zinc-500">{description}</div>
      </div>
      <span
        className={`text-[0.72rem] font-semibold ${ok ? 'text-emerald-400' : 'text-zinc-600'}`}
      >
        {ok ? 'configured' : 'not set'}
      </span>
    </div>
  );
}

// ── Stat card ─────────────────────────────────────────────────────────────────

function StatCard({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="flex flex-col gap-1 rounded border border-graphite-800 bg-graphite-900 px-5 py-4">
      <div className="text-[0.72rem] font-semibold uppercase tracking-wide text-zinc-500">
        {label}
      </div>
      <div className="text-2xl font-semibold text-zinc-100">
        {typeof value === 'number' ? value.toLocaleString() : value}
      </div>
    </div>
  );
}

// ── MB coverage bar ───────────────────────────────────────────────────────────

function CoverageBar({ label, present, total }: { label: string; present: number; total: number }) {
  const pct = total > 0 ? Math.round((present / total) * 100) : 0;
  const color = pct >= 90 ? 'success' : pct >= 60 ? 'warning' : 'error';
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between text-[0.78rem]">
        <span className="text-zinc-300">{label}</span>
        <span className="tabular-nums text-zinc-400">
          {present.toLocaleString()} / {total.toLocaleString()} ({pct}%)
        </span>
      </div>
      <LinearProgress
        variant="determinate"
        value={pct}
        color={color}
        sx={{ height: 6, borderRadius: 3, backgroundColor: '#1e293b' }}
      />
    </div>
  );
}

type ConfigAction = 'save' | 'revert' | null;

function formatBackupTime(value?: number | null): string {
  if (!value) return 'No backup';
  return `Backup from ${new Date(value * 1000).toLocaleString()}`;
}

function ConfigActionDialog({
  action,
  onClose,
  onConfirm,
  busy,
}: {
  action: ConfigAction;
  onClose: () => void;
  onConfirm: () => void;
  busy: boolean;
}) {
  const isSave = action === 'save';
  return (
    <Dialog open={action !== null} onClose={busy ? () => undefined : onClose} className="relative z-50">
      <DialogBackdrop className="fixed inset-0 bg-graphite-950/60" />
      <div className="fixed inset-0 flex items-center justify-center p-4">
        <DialogPanel className="w-full max-w-sm rounded-lg border border-graphite-700 bg-graphite-900 p-5 shadow-2xl">
          <DialogTitle className="text-base font-semibold text-zinc-100">
            {isSave ? 'Save config.yaml?' : 'Revert config.yaml?'}
          </DialogTitle>
          <p className="mt-2 text-sm text-zinc-400">
            {isSave
              ? 'The current file will be backed up before the new content is written.'
              : 'The backup file will replace the current config.yaml.'}
          </p>
          <div className="mt-5 flex justify-end gap-2">
            <Button variant="outlined" size="small" onClick={onClose} disabled={busy}>
              Cancel
            </Button>
            <Button
              variant="contained"
              size="small"
              color={isSave ? 'primary' : 'warning'}
              onClick={onConfirm}
              disabled={busy}
            >
              {busy ? 'Working...' : isSave ? 'Save' : 'Revert'}
            </Button>
          </div>
        </DialogPanel>
      </div>
    </Dialog>
  );
}

// ── ID health metric ──────────────────────────────────────────────────────────

function HealthMetric({
  label,
  value,
  tone = 'neutral',
}: {
  label: string;
  value: number;
  tone?: 'neutral' | 'warn' | 'ok';
}) {
  const color = tone === 'ok' ? 'text-emerald-300' : tone === 'warn' ? 'text-amber-300' : 'text-zinc-100';
  return (
    <div className="rounded border border-graphite-800 bg-graphite-950/35 px-3 py-2">
      <div className="text-[0.68rem] font-semibold uppercase tracking-wide text-zinc-500">
        {label}
      </div>
      <div className={`mt-1 text-lg font-semibold tabular-nums ${color}`}>
        {value.toLocaleString()}
      </div>
    </div>
  );
}

const MUSIC_FORMAT_LAYOUTS: Array<{ key: MusicFormatLayout; label: string }> = [
  { key: 'mono', label: 'Mono' },
  { key: 'stereo', label: 'Stereo / 2.0' },
  { key: '2.1', label: '2.1' },
  { key: '5.1', label: '5.1' },
  { key: '7.1', label: '7.1' },
  { key: 'atmos', label: 'Atmos' },
];

const MUSIC_FORMATS: Array<{ key: MusicFormatKey; label: string }> = [
  { key: 'flac', label: 'FLAC' },
  { key: 'mp3', label: 'MP3' },
  { key: 'aac', label: 'AAC' },
  { key: 'alac', label: 'ALAC' },
  { key: 'opus', label: 'Opus' },
  { key: 'wav', label: 'WAV' },
  { key: 'eac3', label: 'E-AC-3 / Atmos' },
  { key: 'truehd', label: 'TrueHD / Atmos' },
];

const DEFAULT_MUSIC_FORMAT_PREFS: MusicFormatPreferences = {
  allowed_layouts: { mono: false, stereo: true, '2.1': true, '5.1': false, '7.1': false, atmos: true },
  allow_atmos: true,
  custom_max_channels: null,
  preferred_formats: ['flac', 'mp3', 'aac', 'eac3', 'truehd'],
  rejected_download_handling: 'quarantine',
  replacement_fallback: {
    keep_current: true,
    mark_needs_replacement: true,
    queue_retry: true,
    try_lower_ranked: true,
    try_alternate_source: true,
    allow_temporary_exception: false,
  },
};

function formatLabel(key: MusicFormatKey) {
  return MUSIC_FORMATS.find((item) => item.key === key)?.label ?? key.toUpperCase();
}
// ── Page ──────────────────────────────────────────────────────────────────────

export default function Config() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [stats, setStats] = useState<StatsResponse | null>(null);
  const [mbStatus, setMbStatus] = useState<MbidStatusResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [configText, setConfigText] = useState('');
  const [savedConfigText, setSavedConfigText] = useState('');
  const [configMeta, setConfigMeta] = useState<Pick<ConfigFileResponse, 'has_backup' | 'backup_ts'> | null>(null);
  const [configLoading, setConfigLoading] = useState(true);
  const [configBusy, setConfigBusy] = useState(false);
  const [configError, setConfigError] = useState('');
  const [configMsg, setConfigMsg] = useState('');
  const [configAction, setConfigAction] = useState<ConfigAction>(null);
  const [formatPrefs, setFormatPrefs] = useState<MusicFormatPreferences>(DEFAULT_MUSIC_FORMAT_PREFS);
  const [savedFormatPrefs, setSavedFormatPrefs] = useState<MusicFormatPreferences>(DEFAULT_MUSIC_FORMAT_PREFS);
  const [replacementRows, setReplacementRows] = useState<MusicFormatReplacementTrack[]>([]);
  const [formatLoading, setFormatLoading] = useState(true);
  const [formatBusy, setFormatBusy] = useState(false);
  const [formatMsg, setFormatMsg] = useState('');
  const [formatError, setFormatError] = useState('');

  const configDirty = configText !== savedConfigText;
  const configEmpty = !configText.trim();
  const formatDirty = JSON.stringify(formatPrefs) !== JSON.stringify(savedFormatPrefs);
  const queuedReplacementCount = replacementRows.filter((row) => {
    const status = (row.replacement_status || row.status || '').toLowerCase();
    return row.queued_retry !== false && !status.includes('complete');
  }).length;

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [h, s, m] = await Promise.all([getHealth(), getStats(), getMbidStatus()]);
      setHealth(h);
      setStats(s);
      setMbStatus(m);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  const loadConfig = useCallback(async () => {
    setConfigLoading(true);
    setConfigError('');
    try {
      const cfg = await getConfigFile();
      const content = cfg.content ?? '';
      setConfigText(content);
      setSavedConfigText(content);
      setConfigMeta({ has_backup: Boolean(cfg.has_backup), backup_ts: cfg.backup_ts ?? null });
    } catch (err) {
      setConfigError(err instanceof Error ? err.message : String(err));
    } finally {
      setConfigLoading(false);
    }
  }, []);

  const loadMusicFormat = useCallback(async () => {
    setFormatLoading(true);
    setFormatError('');
    try {
      const [prefs, statuses] = await Promise.all([
        getMusicFormatPreferences(),
        getMusicFormatReplacementStatuses(),
      ]);
      setFormatPrefs(prefs.preferences);
      setSavedFormatPrefs(prefs.preferences);
      setReplacementRows(statuses.tracks ?? []);
    } catch (err) {
      setFormatError(err instanceof Error ? err.message : String(err));
    } finally {
      setFormatLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    void loadConfig();
    void loadMusicFormat();
  }, [load, loadConfig, loadMusicFormat]);

  const handleSaveMusicFormat = async () => {
    setFormatBusy(true);
    setFormatMsg('');
    setFormatError('');
    try {
      const saved = await saveMusicFormatPreferences(formatPrefs);
      setFormatPrefs(saved.preferences);
      setSavedFormatPrefs(saved.preferences);
      setFormatMsg('Saved Music Format Preferences.');
    } catch (err) {
      setFormatError(err instanceof Error ? err.message : String(err));
    } finally {
      setFormatBusy(false);
    }
  };

  const handleStartMusicFormatScan = async () => {
    setFormatBusy(true);
    setFormatMsg('');
    setFormatError('');
    try {
      const job = await startMusicFormatScan();
      setFormatMsg(`Music format scan queued in Jobs: ${job.job_id}`);
    } catch (err) {
      setFormatError(err instanceof Error ? err.message : String(err));
    } finally {
      setFormatBusy(false);
    }
  };

  const handleStartMusicFormatReplacement = async () => {
    setFormatBusy(true);
    setFormatMsg('');
    setFormatError('');
    try {
      const job = await startMusicFormatReplacement();
      setFormatMsg(`Music format replacement retry queued in Jobs: ${job.job_id}`);
    } catch (err) {
      setFormatError(err instanceof Error ? err.message : String(err));
    } finally {
      setFormatBusy(false);
    }
  };

  const handleSaveConfig = async () => {
    setConfigBusy(true);
    setConfigMsg('');
    setConfigError('');
    try {
      const saved = await saveConfigFile(configText);
      await loadConfig();
      setConfigMsg(saved.backed_up ? 'Saved config.yaml. Backup updated.' : 'Saved config.yaml.');
    } catch (err) {
      setConfigError(err instanceof Error ? err.message : String(err));
    } finally {
      setConfigBusy(false);
      setConfigAction(null);
    }
  };

  const handleRevertConfig = async () => {
    setConfigBusy(true);
    setConfigMsg('');
    setConfigError('');
    try {
      await revertConfigFile();
      await loadConfig();
      setConfigMsg('Reverted config.yaml from backup.');
    } catch (err) {
      setConfigError(err instanceof Error ? err.message : String(err));
    } finally {
      setConfigBusy(false);
      setConfigAction(null);
    }
  };

  const toggleLayout = (key: MusicFormatLayout, checked: boolean) => {
    setFormatPrefs((current) => ({
      ...current,
      allow_atmos: key === 'atmos' ? checked : current.allow_atmos,
      allowed_layouts: { ...current.allowed_layouts, [key]: checked },
    }));
  };

  const toggleFormat = (key: MusicFormatKey, checked: boolean) => {
    setFormatPrefs((current) => ({
      ...current,
      preferred_formats: checked
        ? [...current.preferred_formats.filter((item) => item !== key), key]
        : current.preferred_formats.filter((item) => item !== key),
    }));
  };

  const moveFormat = (key: MusicFormatKey, direction: -1 | 1) => {
    setFormatPrefs((current) => {
      const next = [...current.preferred_formats];
      const index = next.indexOf(key);
      const target = index + direction;
      if (index < 0 || target < 0 || target >= next.length) return current;
      [next[index], next[target]] = [next[target], next[index]];
      return { ...current, preferred_formats: next };
    });
  };

  const setFallback = (key: keyof MusicFormatPreferences['replacement_fallback'], checked: boolean) => {
    setFormatPrefs((current) => ({
      ...current,
      replacement_fallback: { ...current.replacement_fallback, [key]: checked },
    }));
  };

  const refreshAll = () => {
    void load();
    void loadConfig();
    void loadMusicFormat();
  };

  const itemReleaseGapRows = mbStatus?.item_release_gap_rows ?? 0;
  const trackRecordingGapRows = mbStatus?.track_recording_gap_rows ?? mbStatus?.missing_track_mb ?? 0;
  const inferredAlbumRows = mbStatus?.inferred_album_mbid_rows ?? 0;
  const templateTokenRows = mbStatus?.template_token_rows ?? 0;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between gap-4">
        <h1 className="text-xl font-semibold text-zinc-100">Config</h1>
        <Button variant="outlined" size="small" onClick={refreshAll} disabled={loading || configLoading || formatLoading}>
          Refresh
        </Button>
      </div>

      {error && <Alert severity="error">{error}</Alert>}
      {loading && <LinearProgress sx={{ borderRadius: 1 }} />}

      {/* Library stats */}
      <section className="space-y-3">
        <h2 className="text-[0.78rem] font-semibold uppercase tracking-wide text-zinc-500">Library</h2>
        {stats && (
          <div className="grid grid-cols-3 gap-3">
            <StatCard label="Artists" value={stats.artists} />
            <StatCard label="Albums"  value={stats.albums} />
            <StatCard label="Tracks"  value={stats.tracks} />
          </div>
        )}
      </section>

      {/* MusicBrainz coverage */}
      {mbStatus?.ok && (
        <section className="space-y-3">
          <h2 className="text-[0.78rem] font-semibold uppercase tracking-wide text-zinc-500">MusicBrainz</h2>
          <div className="rounded border border-graphite-800 bg-graphite-900 px-5 py-4 space-y-4">
            <CoverageBar
              label="Albums with MB release ID"
              present={mbStatus.total_albums - mbStatus.missing_album_mb}
              total={mbStatus.total_albums}
            />
            <CoverageBar
              label="Tracks with MB recording ID"
              present={mbStatus.total_tracks - mbStatus.missing_track_mb}
              total={mbStatus.total_tracks}
            />
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
              <HealthMetric label="Item release gaps"  value={itemReleaseGapRows}      tone={itemReleaseGapRows ? 'warn' : 'ok'} />
              <HealthMetric label="Recording ID gaps"  value={trackRecordingGapRows}   tone={trackRecordingGapRows ? 'warn' : 'ok'} />
              <HealthMetric label="Inferable albums"   value={inferredAlbumRows}        tone={inferredAlbumRows ? 'warn' : 'ok'} />
              <HealthMetric label="Path token rows"    value={templateTokenRows}        tone={templateTokenRows ? 'warn' : 'ok'} />
            </div>
          </div>
        </section>
      )}

      {/* Integration status */}
      {health && (
        <section className="space-y-3">
          <h2 className="text-[0.78rem] font-semibold uppercase tracking-wide text-zinc-500">Integrations</h2>
          <div className="grid gap-2 sm:grid-cols-2">
            {INTEGRATIONS.map(({ key, label, description }) => (
              <IntegrationRow key={key} label={label} description={description} ok={health.checks[key]} />
            ))}
          </div>
        </section>
      )}

      {/* Music format preferences */}
      <section className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="text-[0.78rem] font-semibold uppercase tracking-wide text-zinc-500">Music Format Preferences</h2>
            <p className="mt-1 text-xs text-zinc-500">
              Downloaded tracks are inspected before import. Files that do not match your selected format preferences are rejected or replaced.
            </p>
          </div>
          <div className="flex gap-2">
            <Button size="small" variant="outlined" onClick={() => void loadMusicFormat()} disabled={formatLoading || formatBusy}>Reload</Button>
            <Button size="small" variant="outlined" onClick={() => void handleStartMusicFormatScan()} disabled={formatLoading || formatBusy}>Scan library</Button>
            <Button size="small" variant="outlined" color="warning" onClick={() => void handleStartMusicFormatReplacement()} disabled={formatLoading || formatBusy || queuedReplacementCount === 0}>Replace queued</Button>
            <Button size="small" variant="contained" onClick={() => void handleSaveMusicFormat()} disabled={formatLoading || formatBusy || !formatDirty || formatPrefs.preferred_formats.length === 0}>Save</Button>
          </div>
        </div>
        {formatMsg && <Alert severity="success" onClose={() => setFormatMsg('')}>{formatMsg}</Alert>}
        {formatError && <Alert severity="error">{formatError}</Alert>}
        {formatLoading && <LinearProgress sx={{ borderRadius: 1 }} />}
        <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
          <div className="rounded border border-graphite-800 bg-graphite-900 p-4">
            <div className="text-sm font-medium text-zinc-200">Audio layouts</div>
            <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {MUSIC_FORMAT_LAYOUTS.map((layout) => (
                <label key={layout.key} className="flex items-center gap-2 text-sm text-zinc-300">
                  <input
                    type="checkbox"
                    checked={Boolean(formatPrefs.allowed_layouts[layout.key])}
                    onChange={(event) => toggleLayout(layout.key, event.target.checked)}
                  />
                  {layout.label}
                </label>
              ))}
            </div>
            <div className="mt-4 grid gap-3 sm:grid-cols-2">
              <label className="text-xs text-zinc-500">
                Custom max channel count
                <TextField
                  fullWidth
                  size="small"
                  type="number"
                  value={formatPrefs.custom_max_channels ?? ''}
                  onChange={(event) => {
                    const value = Number(event.target.value);
                    setFormatPrefs((current) => ({ ...current, custom_max_channels: Number.isFinite(value) && value > 0 ? value : null }));
                  }}
                  slotProps={{ htmlInput: { min: 1, max: 32 } }}
                />
              </label>
              <div className="space-y-2 text-sm text-zinc-300">
                <label className="flex items-center gap-2">
                  <input
                    name="atmos-mode"
                    type="radio"
                    checked={formatPrefs.allow_atmos}
                    onChange={() => setFormatPrefs((current) => ({ ...current, allow_atmos: true, allowed_layouts: { ...current.allowed_layouts, atmos: true } }))}
                  />
                  Allow Atmos audio
                </label>
                <label className="flex items-center gap-2">
                  <input
                    name="atmos-mode"
                    type="radio"
                    checked={!formatPrefs.allow_atmos}
                    onChange={() => setFormatPrefs((current) => ({ ...current, allow_atmos: false, allowed_layouts: { ...current.allowed_layouts, atmos: false } }))}
                  />
                  Reject Atmos audio
                </label>
              </div>
            </div>
          </div>

          <div className="rounded border border-graphite-800 bg-graphite-900 p-4">
            <div className="text-sm font-medium text-zinc-200">Preferred formats</div>
            <div className="mt-3 space-y-2">
              {MUSIC_FORMATS.map((format) => {
                const enabled = formatPrefs.preferred_formats.includes(format.key);
                const rank = formatPrefs.preferred_formats.indexOf(format.key);
                return (
                  <div key={format.key} className="flex items-center justify-between gap-2 rounded border border-graphite-800 bg-graphite-950/35 px-3 py-2 text-sm">
                    <label className="flex items-center gap-2 text-zinc-300">
                      <input type="checkbox" checked={enabled} onChange={(event) => toggleFormat(format.key, event.target.checked)} />
                      {format.label}
                    </label>
                    {enabled ? (
                      <div className="flex items-center gap-1 text-xs text-zinc-500">
                        <span>#{rank + 1}</span>
                        <Button size="small" variant="text" onClick={() => moveFormat(format.key, -1)} disabled={rank <= 0}>Up</Button>
                        <Button size="small" variant="text" onClick={() => moveFormat(format.key, 1)} disabled={rank < 0 || rank >= formatPrefs.preferred_formats.length - 1}>Down</Button>
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>
            <div className="mt-3 text-xs text-zinc-500">
              Current order: {formatPrefs.preferred_formats.map(formatLabel).join(' -> ') || 'Select at least one format'}
            </div>
          </div>
        </div>

        <div className="grid gap-3 xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
          <div className="rounded border border-graphite-800 bg-graphite-900 p-4">
            <div className="text-sm font-medium text-zinc-200">Rejected audio handling</div>
            <div className="mt-3 grid gap-3 sm:grid-cols-2">
              <label className="text-xs text-zinc-500">
                Rejected downloads
                <select
                  className="mt-1 w-full rounded border border-graphite-700 bg-graphite-950 px-2 py-2 text-sm text-zinc-200"
                  value={formatPrefs.rejected_download_handling}
                  onChange={(event) => setFormatPrefs((current) => ({ ...current, rejected_download_handling: event.target.value as MusicFormatPreferences['rejected_download_handling'] }))}
                >
                  <option value="quarantine">Quarantine rejected files</option>
                  <option value="delete">Delete rejected files</option>
                </select>
              </label>
              <div className="space-y-2 text-sm text-zinc-300">
                <label className="flex items-center gap-2">
                  <input type="checkbox" checked={formatPrefs.replacement_fallback.keep_current} disabled />
                  Keep current file
                </label>
                <label className="flex items-center gap-2">
                  <input type="checkbox" checked={formatPrefs.replacement_fallback.queue_retry} onChange={(event) => setFallback('queue_retry', event.target.checked)} />
                  Queue automatic retry
                </label>
                <label className="flex items-center gap-2">
                  <input type="checkbox" checked={formatPrefs.replacement_fallback.try_lower_ranked} onChange={(event) => setFallback('try_lower_ranked', event.target.checked)} />
                  Try lower-ranked allowed formats
                </label>
                <label className="flex items-center gap-2">
                  <input type="checkbox" checked={formatPrefs.replacement_fallback.try_alternate_source} onChange={(event) => setFallback('try_alternate_source', event.target.checked)} />
                  Try alternate source
                </label>
              </div>
            </div>
          </div>

          <div className="rounded border border-graphite-800 bg-graphite-900 p-4">
            <div className="flex items-center justify-between gap-3">
              <div className="text-sm font-medium text-zinc-200">Replacement status</div>
              <span className="text-xs text-zinc-500">{queuedReplacementCount.toLocaleString()} queued / {replacementRows.length.toLocaleString()} tracked</span>
            </div>
            <div className="mt-3 space-y-2">
              {replacementRows.slice(0, 5).map((row) => (
                <div key={`${row.item_id ?? row.path}-${row.updated_at ?? ''}`} className="rounded border border-graphite-800 bg-graphite-950/35 px-3 py-2 text-sm">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="font-medium text-zinc-200">{row.artist || 'Unknown artist'} - {row.title || 'Unknown track'}</span>
                    <span className="text-xs text-amber-300">{row.replacement_status || row.status}</span>
                  </div>
                  {row.reason ? <div className="mt-1 text-xs text-zinc-500">{row.reason}</div> : null}
                  {(row.failure_stage || row.attempt_count || row.next_retry_at) ? (
                    <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[0.7rem] text-zinc-500">
                      {row.failure_stage ? <span>Stage: {row.failure_stage.replace(/_/g, ' ')}</span> : null}
                      {row.attempt_count ? <span>Attempts: {row.attempt_count}</span> : null}
                      {row.next_retry_at ? <span>Retry: {new Date(row.next_retry_at * 1000).toLocaleString()}</span> : null}
                    </div>
                  ) : null}
                </div>
              ))}
              {replacementRows.length === 0 ? <div className="text-sm text-zinc-500">No tracks are marked Needs replacement.</div> : null}
            </div>
          </div>
        </div>
      </section>
      {/* Plugin controls */}
      <section className="space-y-3">
        <h2 className="text-[0.78rem] font-semibold uppercase tracking-wide text-zinc-500">Plugins</h2>
        <PluginsPanel />
      </section>

      {/* config.yaml */}
      <section className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="text-[0.78rem] font-semibold uppercase tracking-wide text-zinc-500">config.yaml</h2>
            <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-zinc-500">
              <code className="text-zinc-400">/config/config.yaml</code>
              <span>{formatBackupTime(configMeta?.backup_ts)}</span>
              {configDirty && <span className="font-semibold text-amber-400">Unsaved</span>}
            </div>
          </div>
          <div className="flex gap-2">
            <Button size="small" variant="outlined" onClick={loadConfig} disabled={configLoading || configBusy}>Reload</Button>
            <Button size="small" variant="outlined" color="warning" onClick={() => setConfigAction('revert')} disabled={configLoading || configBusy || !configMeta?.has_backup}>Revert</Button>
            <Button size="small" variant="contained" onClick={() => setConfigAction('save')} disabled={configLoading || configBusy || !configDirty || configEmpty}>Save</Button>
          </div>
        </div>
        {configMsg && <Alert severity="success" onClose={() => setConfigMsg('')}>{configMsg}</Alert>}
        {configError && <Alert severity="error">{configError}</Alert>}
        {configLoading && <LinearProgress sx={{ borderRadius: 1 }} />}
        <TextField
          value={configText}
          onChange={(event) => setConfigText(event.target.value)}
          fullWidth
          multiline
          minRows={10}
          maxRows={20}
          disabled={configLoading || configBusy}
          spellCheck={false}
          slotProps={{
            input: {
              sx: {
                alignItems: 'flex-start',
                bgcolor: '#020617',
                color: '#d1d5db',
                fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace',
                fontSize: '0.78rem',
                lineHeight: 1.55,
                overflowY: 'auto',
              },
            },
          }}
        />
      </section>

      <ConfigActionDialog
        action={configAction}
        onClose={() => setConfigAction(null)}
        onConfirm={configAction === 'save' ? handleSaveConfig : handleRevertConfig}
        busy={configBusy}
      />
    </div>
  );
}
