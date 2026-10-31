import { useInterval } from '../lib/hooks';
import { errorMessage } from '../lib/errors';
import { Tab, TabGroup, TabList, TabPanel, TabPanels } from '@headlessui/react';
import Chip from '@mui/material/Chip';
import LinearProgress from '@mui/material/LinearProgress';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Navigate, useNavigate, useSearchParams } from 'react-router';
import { apiJson, getAcquisitionQueue, getLibraryMissingMbIds, getReviewQueue } from '../api/client';
import type { AcquisitionQueueCache } from '../api/types';
import { AcquisitionPanel, type SourceFilter } from '../features/acquisition/AcquisitionPanel';
import { HistoryPanel } from '../features/history/HistoryPanel';
import { ImportReviewPage } from '../features/importReview/ImportReviewPage';
import type { QueueFilter } from '../features/importReview/ImportReviewPage';
import { IntakePanel } from '../features/intake/IntakePanel';
import type { Job, JobListResponse } from '../types/api';

const TABS = [
  { id: 'intake', label: 'Import Source' },
  { id: 'acquire', label: 'Find Music' },
  { id: 'review', label: 'Review Queue' },
  { id: 'history', label: 'Recently Imported' },
] as const;

const TAB_NAMES = TABS.map((tab) => tab.id);
type TabId = (typeof TABS)[number]['id'];
const REVIEW_FILTERS: QueueFilter[] = ['all', 'pending_ai', 'skipped'];
const SOURCE_FILTERS: SourceFilter[] = ['all', 'beets', 'lidarr', 'merged', 'unmonitored'];
export const MISSING_IDS_PATH = '/library/missing-ids';

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
};

function cx(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(' ');
}

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
  };
}

function tabBadge(tabId: string, summary: SummaryState) {
  if (summary.loading) return '';
  if (tabId === 'intake') return summary.runningJobs ? compact(summary.runningJobs) : '';
  if (tabId === 'acquire') return summary.acquireTotal ? compact(summary.acquireTotal) : '';
  if (tabId === 'review') return summary.reviewTotal ? compact(summary.reviewTotal) : '';
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
  const navigate = useNavigate();
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
    } else if (tabId === 'history') {
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
  // moved to Library > Missing IDs.
  if (movedMissingIdsLink) return <Navigate to={MISSING_IDS_PATH} replace />;

  return (
    <section className="space-y-4">
      <div className="rounded-md border border-graphite-800 bg-graphite-950/45 px-4 py-3">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div>
            <p className="text-xs font-semibold uppercase text-red-400">Import</p>
            <h1 className="mt-1 text-2xl font-semibold text-zinc-100">Import music</h1>
            <div className="mt-2 flex flex-wrap gap-2">
              <Chip
                color={summary.runningJobs ? 'info' : summary.partial ? 'warning' : 'default'}
                label={summary.loading ? 'Loading...' : nextAction}
                size="small"
                variant="outlined"
              />
              {summary.reviewTotal ? <Chip color="warning" label={`${compact(summary.reviewTotal)} to review`} size="small" variant="outlined" /> : null}
              {summary.missingMusic ? <Chip color="info" label={`${compact(summary.missingMusic)} missing`} size="small" variant="outlined" /> : null}
              {summary.missingMb ? (
                <Chip
                  clickable
                  color="error"
                  label={`${compact(summary.missingMb)} need IDs`}
                  size="small"
                  variant="outlined"
                  title="Library albums and tracks missing MusicBrainz IDs"
                  onClick={() => navigate(MISSING_IDS_PATH)}
                />
              ) : null}
              {summary.failedJobs ? <Chip color="error" label={`${compact(summary.failedJobs)} failed`} size="small" variant="outlined" /> : null}
              {summary.latestJob ? <Chip label={summary.latestJob} size="small" variant="outlined" /> : null}
            </div>
          </div>
          {summary.merged ? (
            <div className="text-xs text-zinc-400 lg:text-right">
              {compact(summary.merged)} queued
            </div>
          ) : null}
        </div>

        {summary.loading ? <LinearProgress sx={{ mt: 1.5, borderRadius: 1 }} /> : null}
        {summary.error ? <div className="mt-2 text-xs text-rose-300">{summary.error}</div> : null}
      </div>

      <TabGroup selectedIndex={selectedIndex} onChange={handleTabChange}>
        <TabList className="grid gap-1 border-b border-graphite-800 sm:flex">
          {TABS.map(({ id, label }) => {
            const badge = tabBadge(id, summary);
            return (
              <Tab
                key={id}
                className={({ selected }) =>
                  cx(
                    'flex min-h-11 items-center justify-between gap-2 border-b-2 px-4 py-2.5 text-[0.82rem] font-medium outline-none transition-colors sm:justify-start',
                    selected
                      ? 'border-red-400 text-zinc-100'
                      : 'border-transparent text-zinc-400 hover:border-graphite-700 hover:text-zinc-200',
                  )
                }
              >
                <span>{label}</span>
                {badge ? (
                  <span className="rounded border border-graphite-700 px-1.5 py-0.5 text-[0.66rem] font-semibold text-zinc-300">
                    {badge}
                  </span>
                ) : null}
              </Tab>
            );
          })}
        </TabList>

        <TabPanels className="mt-5">
          <TabPanel>
            <IntakePanel onJobStarted={() => loadSummary(true)} />
          </TabPanel>

          <TabPanel>
            <AcquisitionPanel initialSourceFilter={sourceParam} onSourceFilterChange={syncAcquireSource} />
          </TabPanel>

          <TabPanel unmount={false}>
            <ImportReviewPage active={TABS[selectedIndex]?.id === 'review'} initialFilter={filterParam} initialQuery={queryParam} onFilterChange={syncReviewFilter} />
          </TabPanel>

          <TabPanel>
            <HistoryPanel />
          </TabPanel>
        </TabPanels>
      </TabGroup>
    </section>
  );
}
