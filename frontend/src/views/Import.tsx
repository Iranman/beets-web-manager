import { Tab, TabGroup, TabList, TabPanel, TabPanels } from '@headlessui/react';
import Chip from '@mui/material/Chip';
import LinearProgress from '@mui/material/LinearProgress';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { apiJson, getAcquisitionQueue, getReviewQueue } from '../api/client';
import type { AcquisitionQueueResponse, ReviewQueueResponse } from '../api/types';
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
const REVIEW_FILTERS: QueueFilter[] = ['all', 'pending_ai', 'skipped', 'library_no_mb'];
const SOURCE_FILTERS: SourceFilter[] = ['all', 'beets', 'lidarr'];
const REVIEW_SUMMARY_LIMIT = 5000;

type SummaryState = {
  loading: boolean;
  error: string;
  acquireTotal: number;
  missingMusic: number;
  lidarrWanted: number;
  merged: number;
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

export default function Import() {
  const [searchParams, setSearchParams] = useSearchParams();
  const tabParam = searchParams.get('tab') ?? '';
  const filterParam = reviewFilterFromParam(searchParams.get('filter') ?? 'all');
  const sourceParam = sourceFilterFromParam(searchParams.get('source'));
  const fallbackTab: TabId = searchParams.has('source') ? 'acquire' : 'intake';
  const requestedTab = TAB_NAMES.includes(tabParam as TabId) ? (tabParam as TabId) : fallbackTab;
  const defaultIndex = Math.max(0, TAB_NAMES.indexOf(requestedTab));
  const [selectedIndex, setSelectedIndex] = useState(defaultIndex);
  const [summary, setSummary] = useState<SummaryState>(EMPTY_SUMMARY);

  useEffect(() => {
    setSelectedIndex(defaultIndex);
  }, [defaultIndex]);

  const loadSummary = useCallback(async (force = false, silent = false) => {
    if (!silent) {
      setSummary((current) => ({ ...current, loading: true, error: '' }));
    }
    const [acquireResult, reviewResult, jobsResult] = await Promise.allSettled([
      getAcquisitionQueue(force),
      getReviewQueue({ limit: REVIEW_SUMMARY_LIMIT }),
      apiJson<JobListResponse>('/api/jobs'),
    ]);

    const acquire = acquireResult.status === 'fulfilled'
      ? (acquireResult.value as AcquisitionQueueResponse)
      : null;
    const review = reviewResult.status === 'fulfilled'
      ? (reviewResult.value as ReviewQueueResponse)
      : null;
    const jobs = jobsResult.status === 'fulfilled' ? jobsResult.value.jobs ?? [] : [];
    const jobSummary = summarizeJobs(jobs);
    const failures = [acquireResult, reviewResult, jobsResult]
      .filter((result) => result.status === 'rejected')
      .map((result) => (result as PromiseRejectedResult).reason)
      .map((reason) => reason instanceof Error ? reason.message : String(reason))
      .filter(Boolean);

    setSummary({
      loading: false,
      error: failures[0] || '',
      acquireTotal: acquire?.total ?? 0,
      missingMusic: acquire?.counts?.beets ?? 0,
      lidarrWanted: acquire?.counts?.lidarr ?? 0,
      merged: acquire?.counts?.merged ?? 0,
      reviewTotal: review?.total ?? 0,
      pendingAi: review?.counts?.pending_ai ?? 0,
      skipped: review?.counts?.skipped ?? 0,
      missingMb: review?.counts?.library_no_mb ?? 0,
      ...jobSummary,
    });
  }, []);

  useEffect(() => {
    void loadSummary();
  }, [loadSummary]);

  useEffect(() => {
    const intervalId = window.setInterval(() => {
      if (!document.hidden) void loadSummary(false, true);
    }, 45000);
    return () => window.clearInterval(intervalId);
  }, [loadSummary]);

  const nextAction = useMemo(() => {
    if (summary.runningJobs) return 'Jobs running';
    if (summary.reviewTotal) return 'Review tags';
    if (summary.missingMb) return 'Review IDs';
    if (summary.missingMusic || summary.lidarrWanted) return 'Find music';
    return 'Ready';
  }, [summary.lidarrWanted, summary.missingMb, summary.missingMusic, summary.reviewTotal, summary.runningJobs]);

  function handleTabChange(index: number) {
    setSelectedIndex(index);
    const nextTab = TABS[index]?.id || 'intake';
    navigateTo(nextTab);
  }

  function navigateTo(tabId: TabId) {
    setSelectedIndex(Math.max(0, TAB_NAMES.indexOf(tabId)));
    const nextParams = new URLSearchParams(searchParams);
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
    setSearchParams(nextParams, { replace: true });
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

  return (
    <section className="space-y-4">
      <div className="rounded-md border border-graphite-800 bg-graphite-950/45 px-4 py-3">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div>
            <p className="text-xs font-semibold uppercase text-red-400">Import</p>
            <h1 className="mt-1 text-2xl font-semibold text-zinc-100">Import music</h1>
            <div className="mt-2 flex flex-wrap gap-2">
              <Chip color={summary.runningJobs ? 'info' : 'default'} label={nextAction} size="small" variant="outlined" />
              {summary.reviewTotal ? <Chip color="warning" label={`${compact(summary.reviewTotal)} to review`} size="small" variant="outlined" /> : null}
              {summary.missingMusic ? <Chip color="info" label={`${compact(summary.missingMusic)} missing`} size="small" variant="outlined" /> : null}
              {summary.missingMb ? <Chip color="error" label={`${compact(summary.missingMb)} need IDs`} size="small" variant="outlined" /> : null}
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
            <IntakePanel onJobStarted={() => loadSummary(false, true)} />
          </TabPanel>

          <TabPanel>
            <AcquisitionPanel initialSourceFilter={sourceParam} onSourceFilterChange={syncAcquireSource} />
          </TabPanel>

          <TabPanel unmount={false}>
            <ImportReviewPage active={TABS[selectedIndex]?.id === 'review'} initialFilter={filterParam} onFilterChange={syncReviewFilter} />
          </TabPanel>

          <TabPanel>
            <HistoryPanel />
          </TabPanel>
        </TabPanels>
      </TabGroup>
    </section>
  );
}
