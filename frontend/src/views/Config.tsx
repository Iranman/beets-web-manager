import { formatEpochDateTime } from '../lib/dates';
import { errorMessage, panelError } from '../lib/errors';
import { useEngineOffline } from '../components/EngineOfflineBanner';
import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import LinearProgress from '@mui/material/LinearProgress';
import TextField from '@mui/material/TextField';
import { useCallback, useEffect, useRef, useState, type ReactNode, type RefObject } from 'react';
import {
  apiErrorBody,
  getMbidStatus,
  getMusicFormatPreferences,
  getMusicFormatReplacementStatuses,
  getStats,
  saveMusicFormatPreferences,
  startMusicFormatReplacement,
  startMusicFormatScan,
} from '../api/client';
import { PluginsPanel } from '../features/plugins/PluginsPanel';
import { ConfigYamlEditor } from '../features/system/ConfigYamlEditor';
import { SecuritySection } from '../features/system/SecuritySection';
import type {
  MbidStatusResponse,
  MusicFormatKey,
  MusicFormatLayout,
  MusicFormatPreferences,
  MusicFormatReplacementTrack,
  StatsResponse,
} from '../api/types';

// Settings is the one editing page: an index of sections beside the work (sticky from 64rem), each
// section on a rule with a serif title. Library counts sit on the masthead's catalogue line; the
// preferred-format order is the page's one wooden surface.

// ── MB coverage bar ───────────────────────────────────────────────────────────

function CoverageBar({ label, present, total }: { label: string; present: number; total: number }) {
  const pct = total > 0 ? Math.round((present / total) * 100) : 0;
  const color = pct >= 90 ? 'success' : pct >= 60 ? 'warning' : 'error';
  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 text-sm">
        <span className="text-zinc-100">{label}</span>
        <span className="tabular-nums text-zinc-300">
          {present.toLocaleString()} / {total.toLocaleString()} ({pct}%)
        </span>
      </div>
      <LinearProgress
        variant="determinate"
        value={pct}
        color={color}
        aria-label={label}
        sx={{ height: 6, borderRadius: 3, backgroundColor: 'var(--bw-surface-3)' }}
      />
    </div>
  );
}

// ── Section frame ─────────────────────────────────────────────────────────────

/** The in-page index; every entry is a section below and a /config#id deep link. */
const SECTIONS = [
  { id: 'musicbrainz', label: 'MusicBrainz' },
  { id: 'music-format', label: 'Music format' },
  { id: 'plugins', label: 'Plugins' },
  { id: 'config-yaml', label: 'config.yaml' },
  { id: 'security', label: 'Security' },
] as const;

type SectionId = (typeof SECTIONS)[number]['id'];

/** One settings section on a rule: serif title, an optional hint, actions, then the controls. */
function SettingsSection({
  id,
  title,
  hint,
  actions,
  children,
}: {
  id: SectionId;
  title: string;
  hint?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section
      id={id}
      aria-labelledby={`settings-${id}`}
      className="min-w-0 scroll-mt-20 space-y-4 border-t border-graphite-800 pt-6 first:border-t-0 first:pt-0"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 max-w-[65ch]">
          <h2 id={`settings-${id}`} className="ops-section-title">{title}</h2>
          {hint ? <p className="m-0 mt-1 text-sm text-muted">{hint}</p> : null}
        </div>
        {actions ? <div className="flex flex-wrap gap-2">{actions}</div> : null}
      </div>
      {children}
    </section>
  );
}

const SUBHEAD = 'm-0 text-sm font-semibold text-zinc-100';
const CHECK_ROW = 'flex min-h-10 cursor-pointer items-center gap-2 text-sm text-zinc-200 pointer-coarse:min-h-11';
const CHECK = 'size-4 shrink-0 accent-brass-500';

/** Deep links (/config#security): the sections above the target load at their own pace and push it
 * down, so the target is kept at the top while the page settles, until the reader scrolls, clicks
 * or types. Targets carry `scroll-mt-20` to clear the sticky header. */
function useHashTargetScroll(container: RefObject<HTMLElement | null>) {
  useEffect(() => {
    const id = decodeURIComponent(window.location.hash.slice(1));
    if (!id) return undefined;
    const scroll = () => document.getElementById(id)?.scrollIntoView?.({ block: 'start' });
    scroll();
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(scroll);
    if (container.current) observer?.observe(container.current);
    const userEvents = ['wheel', 'touchstart', 'pointerdown', 'keydown'];
    const release = () => {
      observer?.disconnect();
      window.clearTimeout(timer);
      userEvents.forEach((name) => window.removeEventListener(name, release));
    };
    // ponytail: sections that finish loading after 8s are not followed; browser scroll anchoring covers most of those.
    const timer = window.setTimeout(release, 8000);
    userEvents.forEach((name) => window.addEventListener(name, release, { passive: true }));
    return release;
  }, [container]);
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
  const engineOffline = useEngineOffline();
  const [stats, setStats] = useState<StatsResponse | null>(null);
  const [mbStatus, setMbStatus] = useState<MbidStatusResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [formatPrefs, setFormatPrefs] = useState<MusicFormatPreferences>(DEFAULT_MUSIC_FORMAT_PREFS);
  const [savedFormatPrefs, setSavedFormatPrefs] = useState<MusicFormatPreferences>(DEFAULT_MUSIC_FORMAT_PREFS);
  const [replacementRows, setReplacementRows] = useState<MusicFormatReplacementTrack[]>([]);
  const [formatLoading, setFormatLoading] = useState(true);
  // Until real preferences load, the form holds defaults; saving them would overwrite stored values (#609).
  const [formatLoaded, setFormatLoaded] = useState(false);
  // Revision of the loaded preferences; a save from a stale load gets 409 (#623).
  const [formatRevision, setFormatRevision] = useState('');
  const [formatBusy, setFormatBusy] = useState(false);
  const [formatMsg, setFormatMsg] = useState('');
  const [formatError, setFormatError] = useState('');

  const formatDirty = JSON.stringify(formatPrefs) !== JSON.stringify(savedFormatPrefs);
  const queuedReplacementCount = replacementRows.filter((row) => {
    const status = (row.replacement_status || row.status || '').toLowerCase();
    return row.queued_retry !== false && !status.includes('complete');
  }).length;

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [s, m] = await Promise.all([getStats(), getMbidStatus()]);
      setStats(s);
      setMbStatus(m);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
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
      setFormatRevision(prefs.revision);
      setFormatLoaded(true);
      setReplacementRows(statuses.tracks ?? []);
    } catch (err) {
      setFormatError(errorMessage(err));
    } finally {
      setFormatLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    void loadMusicFormat();
  }, [load, loadMusicFormat]);

  // System links here as /config#security, #config-yaml or #plugins; a client-side route change
  // does not scroll to the hash on its own, and the sections above it load after mount.
  const pageRef = useRef<HTMLDivElement>(null);
  useHashTargetScroll(pageRef);

  const handleSaveMusicFormat = async () => {
    setFormatBusy(true);
    setFormatMsg('');
    setFormatError('');
    try {
      const saved = await saveMusicFormatPreferences(formatPrefs, formatRevision);
      setFormatPrefs(saved.preferences);
      setSavedFormatPrefs(saved.preferences);
      setFormatRevision(saved.revision);
      setFormatMsg('Saved Music Format Preferences.');
    } catch (err) {
      // Keep the user's edits in the form; only Reload replaces them.
      setFormatError(apiErrorBody(err)?.code === 'music_format_revision_conflict'
        ? 'Music Format Preferences changed elsewhere (another tab or session), so this save was not applied. Your edits are still in the form. Use Reload to load the latest preferences, then reapply your changes and save.'
        : errorMessage(err));
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
      setFormatError(errorMessage(err));
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
      setFormatError(errorMessage(err));
    } finally {
      setFormatBusy(false);
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
    void loadMusicFormat();
  };

  const itemReleaseGapRows = mbStatus?.item_release_gap_rows ?? 0;
  const trackRecordingGapRows = mbStatus?.track_recording_gap_rows ?? mbStatus?.missing_track_mb ?? 0;
  const inferredAlbumRows = mbStatus?.inferred_album_mbid_rows ?? 0;
  const templateTokenRows = mbStatus?.template_token_rows ?? 0;
  const healthFigures = [
    { label: 'Item release gaps', value: itemReleaseGapRows },
    { label: 'Recording ID gaps', value: trackRecordingGapRows },
    { label: 'Inferable albums', value: inferredAlbumRows },
    { label: 'Path token rows', value: templateTokenRows },
  ];

  return (
    <div ref={pageRef} className="ops-page min-w-0 space-y-8 overflow-x-clip pb-10">
      <header className="ops-masthead">
        <div className="min-w-0">
          <h1 className="ops-title">Settings</h1>
          {stats ? (
            <p className="ops-catalog mt-2">
              <span>{stats.artists.toLocaleString()} artists</span>
              <span>{stats.albums.toLocaleString()} albums</span>
              <span>{stats.tracks.toLocaleString()} tracks</span>
            </p>
          ) : null}
        </div>
        <Button variant="outlined" size="small" onClick={refreshAll} disabled={loading || formatLoading}>
          Refresh
        </Button>
      </header>

      {panelError(error, engineOffline) && <Alert severity="error">{error}</Alert>}
      {loading && <LinearProgress aria-label="Loading library figures" sx={{ borderRadius: 1 }} />}

      <div className="ops-split">
        <nav aria-label="Settings sections" className="ops-split-aside min-w-0">
          <ul className="m-0 flex list-none gap-1 overflow-x-auto border-b border-graphite-800 p-0 lg:flex-col lg:overflow-visible lg:border-b-0 lg:border-l">
            {SECTIONS.map((section) => (
              <li key={section.id} className="shrink-0">
                <a
                  href={`#${section.id}`}
                  className="ops-filter flex items-center whitespace-nowrap no-underline lg:-ml-px lg:border-b-0 lg:border-l-2 lg:border-transparent lg:hover:border-brass-500"
                >
                  {section.label}
                </a>
              </li>
            ))}
          </ul>
        </nav>

        <div className="min-w-0 space-y-10">
          <SettingsSection
            id="musicbrainz"
            title="MusicBrainz"
            hint="How much of the library carries MusicBrainz IDs."
          >
            {mbStatus?.ok ? (
              <div className="space-y-5">
                <div className="grid gap-4 md:grid-cols-2">
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
                </div>
                <dl className="ops-figures">
                  {healthFigures.map((figure) => (
                    <div key={figure.label}>
                      <dt>{figure.label}</dt>
                      <dd className={figure.value ? 'text-amber-300' : 'text-emerald-300'}>{figure.value.toLocaleString()}</dd>
                    </div>
                  ))}
                </dl>
              </div>
            ) : (
              <p className="m-0 text-sm text-muted">
                {loading ? 'Reading MusicBrainz coverage...' : 'MusicBrainz coverage is not available. Use Refresh to try again.'}
              </p>
            )}
          </SettingsSection>

          <SettingsSection
            id="music-format"
            title="Music Format Preferences"
            hint="Downloaded tracks are inspected before import. Files that do not match your selected format preferences are rejected or replaced."
            actions={(
              <>
                <Button size="small" variant="outlined" onClick={() => void loadMusicFormat()} disabled={formatLoading || formatBusy}>Reload</Button>
                <Button size="small" variant="outlined" onClick={() => void handleStartMusicFormatScan()} disabled={formatLoading || formatBusy}>Scan library</Button>
                <Button size="small" variant="outlined" color="warning" onClick={() => void handleStartMusicFormatReplacement()} disabled={formatLoading || formatBusy || queuedReplacementCount === 0}>Replace queued</Button>
                <Button size="small" variant="contained" onClick={() => void handleSaveMusicFormat()} disabled={!formatLoaded || formatLoading || formatBusy || !formatDirty || formatPrefs.preferred_formats.length === 0}>Save</Button>
              </>
            )}
          >
            {formatMsg && <Alert severity="success" onClose={() => setFormatMsg('')}>{formatMsg}</Alert>}
            {panelError(formatError, engineOffline) && <Alert severity="error">{formatError}</Alert>}
            {!formatLoaded && !formatLoading && (
              <Alert severity="warning">
                Your saved Music Format Preferences could not be loaded, so the form is locked to avoid overwriting them with defaults. Use Reload to try again.
              </Alert>
            )}
            {formatLoading && <LinearProgress aria-label="Loading music format preferences" sx={{ borderRadius: 1 }} />}
            {formatDirty && formatLoaded ? <p className="m-0 text-xs font-semibold text-amber-300">Unsaved changes</p> : null}
            <fieldset disabled={!formatLoaded} aria-label="Music format preference fields" className="m-0 min-w-0 space-y-8 border-0 p-0">
              <div className="grid gap-x-10 gap-y-8 xl:grid-cols-2">
                <div className="min-w-0 space-y-3">
                  <h3 className={SUBHEAD}>Preferred formats</h3>
                  <p className="m-0 text-xs text-muted">Tick the formats to accept; the first is tried first.</p>
                  <ul className="crate m-0 list-none divide-y divide-walnut-950 rounded-sm p-0">
                    {MUSIC_FORMATS.map((format) => {
                      const enabled = formatPrefs.preferred_formats.includes(format.key);
                      const rank = formatPrefs.preferred_formats.indexOf(format.key);
                      return (
                        <li key={format.key} className="flex flex-wrap items-center justify-between gap-x-2 px-3">
                          <label className={CHECK_ROW}>
                            <input className={CHECK} type="checkbox" checked={enabled} onChange={(event) => toggleFormat(format.key, event.target.checked)} />
                            {format.label}
                          </label>
                          {enabled ? (
                            <div className="flex items-center gap-1 text-xs text-zinc-300">
                              <span className="w-7 text-right tabular-nums font-semibold text-brass-200">#{rank + 1}</span>
                              <Button size="small" variant="text" aria-label={`Move ${format.label} up`} onClick={() => moveFormat(format.key, -1)} disabled={rank <= 0}>Up</Button>
                              <Button size="small" variant="text" aria-label={`Move ${format.label} down`} onClick={() => moveFormat(format.key, 1)} disabled={rank < 0 || rank >= formatPrefs.preferred_formats.length - 1}>Down</Button>
                            </div>
                          ) : null}
                        </li>
                      );
                    })}
                  </ul>
                  <p className="m-0 text-xs text-zinc-300">
                    Current order: {formatPrefs.preferred_formats.map(formatLabel).join(' -> ') || 'Select at least one format'}
                  </p>
                </div>

                <div className="min-w-0 space-y-6">
                  <div className="space-y-2">
                    <h3 className={SUBHEAD}>Audio layouts</h3>
                    <div className="grid grid-cols-2 gap-x-4 sm:grid-cols-3">
                      {MUSIC_FORMAT_LAYOUTS.map((layout) => (
                        <label key={layout.key} className={CHECK_ROW}>
                          <input
                            className={CHECK}
                            type="checkbox"
                            checked={Boolean(formatPrefs.allowed_layouts[layout.key])}
                            onChange={(event) => toggleLayout(layout.key, event.target.checked)}
                          />
                          {layout.label}
                        </label>
                      ))}
                    </div>
                  </div>
                  <div className="grid gap-4 sm:grid-cols-2">
                    <label className="block space-y-1 text-xs text-zinc-300">
                      <span>Custom max channel count</span>
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
                    <div role="radiogroup" aria-label="Atmos audio">
                      <label className={CHECK_ROW}>
                        <input
                          className={CHECK}
                          name="atmos-mode"
                          type="radio"
                          checked={formatPrefs.allow_atmos}
                          onChange={() => setFormatPrefs((current) => ({ ...current, allow_atmos: true, allowed_layouts: { ...current.allowed_layouts, atmos: true } }))}
                        />
                        Allow Atmos audio
                      </label>
                      <label className={CHECK_ROW}>
                        <input
                          className={CHECK}
                          name="atmos-mode"
                          type="radio"
                          checked={!formatPrefs.allow_atmos}
                          onChange={() => setFormatPrefs((current) => ({ ...current, allow_atmos: false, allowed_layouts: { ...current.allowed_layouts, atmos: false } }))}
                        />
                        Reject Atmos audio
                      </label>
                    </div>
                  </div>

                  <div className="space-y-2 border-t border-graphite-800 pt-5">
                    <h3 className={SUBHEAD}>Rejected audio handling</h3>
                    <div className="grid gap-4 sm:grid-cols-2">
                      <label className="block space-y-1 text-xs text-zinc-300">
                        <span>Rejected downloads</span>
                        <select
                          className="ops-field w-full"
                          value={formatPrefs.rejected_download_handling}
                          onChange={(event) => setFormatPrefs((current) => ({ ...current, rejected_download_handling: event.target.value as MusicFormatPreferences['rejected_download_handling'] }))}
                        >
                          <option value="quarantine">Quarantine rejected files</option>
                          <option value="delete">Delete rejected files</option>
                        </select>
                      </label>
                      <div>
                        <label className={CHECK_ROW}>
                          <input className={CHECK} type="checkbox" checked={formatPrefs.replacement_fallback.keep_current} disabled />
                          Keep current file
                        </label>
                        <label className={CHECK_ROW}>
                          <input className={CHECK} type="checkbox" checked={formatPrefs.replacement_fallback.queue_retry} onChange={(event) => setFallback('queue_retry', event.target.checked)} />
                          Queue automatic retry
                        </label>
                        <label className={CHECK_ROW}>
                          <input className={CHECK} type="checkbox" checked={formatPrefs.replacement_fallback.try_lower_ranked} onChange={(event) => setFallback('try_lower_ranked', event.target.checked)} />
                          Try lower-ranked allowed formats
                        </label>
                        <label className={CHECK_ROW}>
                          <input className={CHECK} type="checkbox" checked={formatPrefs.replacement_fallback.try_alternate_source} onChange={(event) => setFallback('try_alternate_source', event.target.checked)} />
                          Try alternate source
                        </label>
                      </div>
                    </div>
                  </div>
                </div>
              </div>

              <div className="min-w-0 space-y-2">
                <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
                  <h3 className={SUBHEAD}>Replacement status</h3>
                  <span className="text-xs tabular-nums text-zinc-300">{queuedReplacementCount.toLocaleString()} queued / {replacementRows.length.toLocaleString()} tracked</span>
                </div>
                {replacementRows.length === 0 ? (
                  <p className="m-0 border-t border-graphite-800 pt-2 text-sm text-muted">No tracks are marked Needs replacement.</p>
                ) : (
                  <ul className="ops-track-list border-t border-graphite-800">
                    {replacementRows.slice(0, 5).map((row) => (
                      <li key={`${row.item_id ?? row.path}-${row.updated_at ?? ''}`} className="py-2 text-sm">
                        <div className="flex flex-wrap items-baseline justify-between gap-x-2">
                          <span className="min-w-0 break-words font-medium text-zinc-100">{row.artist || 'Unknown artist'} - {row.title || 'Unknown track'}</span>
                          <span className="text-xs font-semibold text-amber-300">{row.replacement_status || row.status}</span>
                        </div>
                        {row.reason ? <div className="mt-1 text-xs text-zinc-300">{row.reason}</div> : null}
                        {(row.failure_stage || row.attempt_count || row.next_retry_at) ? (
                          <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted">
                            {row.failure_stage ? <span>Stage: {row.failure_stage.replace(/_/g, ' ')}</span> : null}
                            {row.attempt_count ? <span>Attempts: {row.attempt_count}</span> : null}
                            {row.next_retry_at ? <span>Retry: {formatEpochDateTime(row.next_retry_at)}</span> : null}
                          </div>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </fieldset>
          </SettingsSection>

          <SettingsSection id="plugins" title="Plugins" hint="Plex Direct and the state of optional Beets command plugins.">
            <PluginsPanel />
          </SettingsSection>

          {/* Beets config.yaml (moved from System) */}
          <SettingsSection id="config-yaml" title="config.yaml" hint="Beets' own configuration. Saving and reverting ask first.">
            <ConfigYamlEditor />
          </SettingsSection>

          {/* Sign-in, API token and outbound policy (moved from System) */}
          <SettingsSection id="security" title="Security">
            <SecuritySection />
          </SettingsSection>
        </div>
      </div>
    </div>
  );
}
