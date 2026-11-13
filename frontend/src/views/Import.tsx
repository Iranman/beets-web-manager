import { useInterval } from '../lib/hooks';
import { errorMessage } from '../lib/errors';
import { Tab, TabGroup, TabList, TabPanel, TabPanels } from '@headlessui/react';
import LinearProgress from '@mui/material/LinearProgress';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Navigate, useSearchParams } from 'react-router';
import { apiJson, getAcquisitionQueue, getLibraryMissingMbIds, getReviewQueue } from '../api/client';
import type { AcquisitionQueueCache } from '../api/types';
import { AcquisitionPanel, type SourceFilter } from '../features/acquisition/AcquisitionPanel';
import { HistoryPanel } from '../features/history/HistoryPanel';
import { ImportReviewPage } from '../features/importReview/ImportReviewPage';
import type { QueueFilter } from '../features/importReview/ImportReviewPage';
import { IntakePanel } from '../features/intake/IntakePanel';
import { PlaylistPipelinePanel } from '../features/playlists/PlaylistPipelinePanel';
import { SleeveArt, albumArtSources } from '../components/SleeveArt';
import type { Job, JobListResponse } from '../types/api';
import LibraryChanges from './LibraryChanges';

const TABS = [
  { id: 'intake', label: 'Import Source' },
  { id: 'acquire', label: 'Find Music' },
  { id: 'playlists', label: 'Playlist Downloads' },
  { id: 'review', label: 'Review Queue' },
  { id: 'history', label: 'Recently Imported' },
  { id: 'missing-ids', label: 'Missing IDs' },
  { id: 'changes', label: 'Library Changes' },
] as const;

const TAB_NAMES = TABS.map((tab) => tab.id);
type TabId = (typeof TABS)[number]['id'];
const REVIEW_FILTERS: QueueFilter[] = ['all', 'pending_ai', 'skipped'];
const SOURCE_FILTERS: SourceFilter[] = ['all', 'beets', 'lidarr', 'merged', 'unmonitored'];
export const MISSING_IDS_PATH = '/import?tab=missing-ids';

type SummaryState = {
  loading: boolean;
  error: string;
  acquireTotal: number;
  missingMusic: number;
  lidarrWanted: number;
  merged: number;
  /** Find Music list state; null when the queue request failed or sent none. */
  acquireCache: AcquisitionQueueCache['state'] | null;
  acquireCacheBuilt: boolean;
  /** At least one summary request failed, so "Ready" cannot be claimed. */
  partial: boolean;
  reviewTotal: number;
  pendingAi: number;
  skipped: number;
  missingMb: number;
  runningJobs: number;
  failedJobs: number;
  latestJob: string;
  /** The newest import jobs, for the sleeves in the arrivals bin (from the same /api/jobs read). */
  arrivals: Job[];
};

const EMPTY_SUMMARY: SummaryState = {
  loading: true,
  error: '',
  acquireTotal: 0,
  missingMusic: 0,
  lidarrWanted: 0,
  merged: 0,
  acquireCache: null,
  acquireCacheBuilt: false,
  partial: false,
  reviewTotal: 0,
  pendingAi: 0,
  skipped: 0,
  missingMb: 0,
  runningJobs: 0,
  failedJobs: 0,
  latestJob: '',
  arrivals: [],
};

function compact(value: number) {
  return new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(value);
}

function reviewFilterFromParam(value: string): QueueFilter {
  return REVIEW_FILTERS.includes(value as QueueFilter) ? (value as QueueFilter) : 'all';
}

function sourceFilterFromParam(value: string | null): SourceFilter {
  return SOURCE_FILTERS.includes(value as SourceFilter) ? (value as SourceFilter) : 'all';
}

function isImportJob(job: Job) {
  const type = String(job.metadata?.type ?? '').toLowerCase();
  const label = String(job.label ?? '').toLowerCase();
  return (
    type.includes('import') ||
    type.includes('acquisition') ||
    type.includes('download') ||
    label.includes('import') ||
    label.includes('acquire') ||
    label.includes('download')
  );
}

function summarizeJobs(jobs: Job[]) {
  const importJobs = jobs.filter(isImportJob);
  return {
    runningJobs: importJobs.filter((job) => job.status === 'running').length,
    failedJobs: importJobs.filter((job) => job.status === 'failed' || job.status === 'killed').length,
    latestJob: importJobs[0]?.label || '',
    arrivals: importJobs.slice(0, 6),
  };
}

/** The album a finished or running import job names, for its sleeve. */
function arrivalArt(job: Job) {
  const meta = (job.metadata ?? {}) as Record<string, unknown>;
  const id = meta.existing_album_id ?? meta.album_id;
  const rgid = typeof meta.mb_releasegroupid === 'string' ? meta.mb_releasegroupid : null;
  return albumArtSources({ albumId: typeof id === 'number' || typeof id === 'string' ? id : null, releaseGroupId: rgid });
}

function tabBadge(tabId: string, summary: SummaryState) {
  if (summary.loading) return '';
  if (tabId === 'intake') return summary.runningJobs ? compact(summary.runningJobs) : '';
  if (tabId === 'acquire') return summary.acquireTotal ? compact(summary.acquireTotal) : '';
  if (tabId === 'review') return summary.reviewTotal ? compact(summary.reviewTotal) : '';
  if (tabId === 'missing-ids') return summary.missingMb ? compact(summary.missingMb) : '';
  return '';
}

/** The summary's next-action label. "Ready" only when every count was read
 * and the Find Music list is built: an empty or building list is not "nothing
 * missing" (QA #390 F3). */
export function importNextAction(summary: SummaryState): string {
  if (summary.runningJobs) return 'Jobs running';
  if (summary.reviewTotal) return 'Review tags';
  if (summary.missingMb) return 'Review IDs';
  if (summary.acquireCache === 'building') return 'Find Music list building';
  if (summary.acquireCache === 'empty') return 'Find Music list not built';
  if (summary.acquireCache === 'failed' && !summary.acquireCacheBuilt) return 'Find Music list failed';
  if (summary.missingMusic || summary.lidarrWanted) return 'Find music';
  if (summary.partial) return 'Status incomplete';
  return 'Ready';
}

export default function Import() {
  const [searchParams, setSearchParams] = useSearchParams();
  const tabParam = searchParams.get('tab') ?? '';
  const filterParam = reviewFilterFromParam(searchParams.get('filter') ?? 'all');
  const queryParam = searchParams.get('q') ?? '';
  const sourceParam = sourceFilterFromParam(searchParams.get('source'));
  const fallbackTab: TabId = searchParams.has('source') ? 'acquire' : 'intake';
  const requestedTab = TAB_NAMES.includes(tabParam as TabId) ? (tabParam as TabId) : fallbackTab;
  const defaultIndex = Math.max(0, TAB_NAMES.indexOf(requestedTab));
  const [selectedIndex, setSelectedIndex] = useState(defaultIndex);
  const [summary, setSummary] = useState<SummaryState>(EMPTY_SUMMARY);
  const movedMissingIdsLink = searchParams.get('filter') === 'library_no_mb';

  useEffect(() => {
    setSelectedIndex(defaultIndex);
  }, [defaultIndex]);

  const loadSummary = useCallback(async (silent = false) => {
    if (!silent) {
      setSummary((current) => ({ ...current, loading: true, error: '' }));
    }
    // The missing-IDs count reads the whole Beets library, so the silent
    // 45-second refresh keeps the last value instead of asking again.
    const [acquireResult, reviewResult, jobsResult, missingResult] = await Promise.allSettled([
      getAcquisitionQueue({ limit: 0 }),
      getReviewQueue({ limit: 0 }),
      apiJson<JobListResponse>('/api/jobs'),
      silent ? Promise.resolve(null) : getLibraryMissingMbIds({ limit: 0 }),
    ]);

    const acquire = acquireResult.status === 'fulfilled' ? acquireResult.value : null;
    const review = reviewResult.status === 'fulfilled' ? reviewResult.value : null;
    const missing = missingResult.status === 'fulfilled' ? missingResult.value : null;
    const jobs = jobsResult.status === 'fulfilled' ? jobsResult.value.jobs ?? [] : [];
    const jobSummary = summarizeJobs(jobs);
    const results = [acquireResult, reviewResult, jobsResult, missingResult];
    const failures = results
      .filter((result) => result.status === 'rejected')
      .map((result) => (result as PromiseRejectedResult).reason)
      .map((reason) => errorMessage(reason))
      .filter(Boolean);

    setSummary((current) => ({
      loading: false,
      error: failures.length ? `Some import counts could not be loaded: ${failures[0]}` : '',
      partial: failures.length > 0,
      acquireTotal: acquire?.total ?? 0,
      missingMusic: acquire?.counts?.beets ?? 0,
      lidarrWanted: acquire?.counts?.lidarr ?? 0,
      merged: acquire?.counts?.merged ?? 0,
      acquireCache: acquire?.cache?.state ?? null,
      acquireCacheBuilt: Boolean(acquire?.cache?.built_at),
      reviewTotal: review?.total ?? 0,
      pendingAi: review?.counts?.pending_ai ?? 0,
      skipped: review?.counts?.skipped ?? 0,
      missingMb: missing ? missing.total ?? 0 : silent ? current.missingMb : 0,
      ...jobSummary,
    }));
  }, []);

  useEffect(() => {
    void loadSummary();
  }, [loadSummary]);

  useInterval(() => {
    if (!document.hidden) void loadSummary(true);
  }, 45000);

  const nextAction = useMemo(() => importNextAction(summary), [summary]);

  function handleTabChange(index: number) {
    setSelectedIndex(index);
    const nextTab = TABS[index]?.id || 'intake';
    navigateTo(nextTab);
  }

  function navigateTo(tabId: TabId) {
    setSelectedIndex(Math.max(0, TAB_NAMES.indexOf(tabId)));
    const nextParams = new URLSearchParams(searchParams);
    nextParams.delete('q');
    // A playlist deep link belongs to Playlist Downloads only.
    if (tabId !== 'playlists') nextParams.delete('playlist');
    // A transaction deep link belongs to Library Changes only.
    if (tabId !== 'changes') nextParams.delete('tx');
    if (tabId === 'intake') {
      nextParams.delete('tab');
      nextParams.delete('filter');
      nextParams.delete('source');
    } else if (tabId === 'acquire') {
      nextParams.set('tab', tabId);
      nextParams.delete('filter');
    } else if (tabId === 'review') {
      nextParams.set('tab', tabId);
      nextParams.delete('source');
    } else if (tabId === 'history' || tabId === 'missing-ids' || tabId === 'changes' || tabId === 'playlists') {
      nextParams.set('tab', tabId);
      nextParams.delete('filter');
      nextParams.delete('source');
    } else {
      nextParams.set('tab', tabId);
    }
    // A tab change is a navigation (Back returns to the previous tab); filter changes below replace.
    setSearchParams(nextParams);
  }

  function syncAcquireSource(nextSource: SourceFilter) {
    const nextParams = new URLSearchParams(searchParams);
    nextParams.set('tab', 'acquire');
    if (nextSource === 'all') nextParams.delete('source');
    else nextParams.set('source', nextSource);
    nextParams.delete('filter');
    setSearchParams(nextParams, { replace: true });
  }

  function syncReviewFilter(nextFilter: QueueFilter) {
    const nextParams = new URLSearchParams(searchParams);
    nextParams.set('tab', 'review');
    if (nextFilter === 'all') nextParams.delete('filter');
    else nextParams.set('filter', nextFilter);
    nextParams.delete('source');
    setSearchParams(nextParams, { replace: true });
  }

  // Old links to the Review Queue's "Needs MusicBrainz ID" filter: those rows
  // live on the Missing IDs tab.
  if (movedMissingIdsLink) return <Navigate to={MISSING_IDS_PATH} replace />;

  return (
    <section className="ops-page space-y-8 pb-10">
      <header className="ops-masthead">
        <div className="min-w-0">
          <h1 className="ops-title">Workbench</h1>
          <p className="mt-2 max-w-prose text-sm text-zinc-300">Import music, review matches, fix missing IDs and approve or roll back library changes.</p>
        </div>
        <p className="ops-catalog" aria-label="Workbench status">
          <span className={summary.runningJobs ? 'text-info' : summary.partial ? 'text-warning' : undefined}>
            {summary.loading ? 'Loading...' : nextAction}
          </span>
          {summary.reviewTotal ? <span className="text-amber-200">{compact(summary.reviewTotal)} to review</span> : null}
          {summary.missingMusic ? <span>{compact(summary.missingMusic)} missing</span> : null}
          {summary.missingMb ? (
            <span>
              <button
                type="button"
                className="min-h-10 text-rose-200 underline underline-offset-4 hover:text-rose-100"
                title="Library albums and tracks missing MusicBrainz IDs"
                onClick={() => navigateTo('missing-ids')}
              >
                {compact(summary.missingMb)} need IDs
              </button>
            </span>
          ) : null}
          {summary.failedJobs ? <span className="text-rose-200">{compact(summary.failedJobs)} failed</span> : null}
          {summary.merged ? <span>{compact(summary.merged)} queued</span> : null}
        </p>
      </header>

      {summary.loading ? <LinearProgress sx={{ borderRadius: 1 }} /> : null}
      {summary.error ? <p className="text-sm text-rose-200">{summary.error}</p> : null}

      <TabGroup selectedIndex={selectedIndex} onChange={handleTabChange}>
        {/* The new-arrivals bin: the latest imports lean at the back as sleeves, the seven sections stand in front as divider cards. */}
        <div className="ops-bin">
          <div className="ops-bin-head">
            <h2 className="ops-section-title">New arrivals</h2>
            {summary.arrivals.length ? (
              <ul className="ops-arrivals" aria-label="Latest imports">
                {summary.arrivals.map((job, index) => (
                  <li key={`${index}-${job.job_id}`} title={job.label}>
                    <SleeveArt size={104} sources={arrivalArt(job)} />
                    <span className="ops-arrival-label">{(job.label ?? '').replace(/^Import:\s*/i, '')}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-zinc-300">{summary.loading ? 'Checking the bin...' : 'Nothing has arrived yet. Start from Import Source or Find Music.'}</p>
            )}
          </div>
          <TabList className="ops-dividers">
            {TABS.map(({ id, label }) => {
              const badge = tabBadge(id, summary);
              return (
                <Tab key={id} className="ops-divider">
                  <span>{label}</span>
                  {badge ? <span className="ops-divider-count">{badge}</span> : null}
                </Tab>
              );
            })}
          </TabList>
        </div>

        <TabPanels className="mt-8">
          <TabPanel>
            <IntakePanel onJobStarted={() => loadSummary(true)} />
          </TabPanel>

          <TabPanel>
            <AcquisitionPanel initialSourceFilter={sourceParam} onSourceFilterChange={syncAcquireSource} />
          </TabPanel>

          <TabPanel>
            <PlaylistPipelinePanel initialPlaylist={searchParams.get('playlist') ?? ''} onJobStarted={() => void loadSummary(true)} />
          </TabPanel>

          <TabPanel unmount={false}>
            <ImportReviewPage active={TABS[selectedIndex]?.id === 'review'} initialFilter={filterParam} initialQuery={queryParam} onFilterChange={syncReviewFilter} />
          </TabPanel>

          <TabPanel>
            <HistoryPanel />
          </TabPanel>

          <TabPanel>
            <section aria-labelledby="missing-ids-title" className="space-y-4">
              <div>
                <h2 id="missing-ids-title" className="font-display text-xl font-semibold text-zinc-100">Missing MusicBrainz IDs</h2>
                <p className="mt-1 max-w-3xl text-sm text-zinc-400">
                  These albums and tracks are already in your Beets library but have no MusicBrainz release or recording ID,
                  so duplicate checks, Find Music, and artwork lookups cannot identify them. Pick a match for each one;
                  nothing changes until you apply it.
                </p>
              </div>
              <ImportReviewPage active mode="missing_ids" />
            </section>
          </TabPanel>

          <TabPanel>
            <LibraryChanges />
          </TabPanel>
        </TabPanels>
      </TabGroup>
    </section>
  );
}
