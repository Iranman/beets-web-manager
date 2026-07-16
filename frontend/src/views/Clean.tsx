import { Tab, TabGroup, TabList, TabPanel, TabPanels } from '@headlessui/react';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import { CleanMetricGrid, CleanPanelHeader } from '../components/CleanPanel';
import { AlbumTracksPanel } from '../features/albumtracks/AlbumTracksPanel';
import { ArtistAliasPanel } from '../features/artistAlias/ArtistAliasPanel';
import { ArtistFoldersPanel } from '../features/artistfolders/ArtistFoldersPanel';
import { DedupPanel } from '../features/dedup/DedupPanel';
import { LibraryHealthPanel } from '../features/libraryHealth/LibraryHealthPanel';
import { apiGet } from '../lib/api';
import type { Job, JobListResponse } from '../types/api';

const TABS = [
  { id: 'library-db', label: 'Library DB', description: 'Database rows, orphaned items, and empty album records.' },
  { id: 'duplicates', label: 'Duplicates', description: 'Duplicate files and library/download copy checks.' },
  { id: 'album-tracks', label: 'Album Tracks', description: 'Extra local tracks, low-confidence matches, and cleanup candidates.' },
  { id: 'artist-alias', label: 'Artist Alias', description: 'Same MusicBrainz artist split across multiple local names.' },
  { id: 'artist-folders', label: 'Artist Folders', description: 'Duplicate folder names and MusicBrainz-ID folder variants.' },
];

const TAB_IDS = TABS.map((tab) => tab.id);
const CLEAN_TAB_STORAGE_KEY = 'beets-clean-selected-tab';
const CLEAN_OVERVIEW_REFRESH_MS = 60_000;

type CleanTabSource = 'query' | 'hash' | 'storage' | 'fallback' | 'default';

interface CleanJobTabRule {
  tabId: string;
  types?: string[];
  phrases?: string[];
}

const CLEAN_JOB_TAB_RULES: CleanJobTabRule[] = [
  {
    tabId: 'library-db',
    types: ['template-token-cleanup', 'mbid-sticking-repair', 'mb-full-sync', 'library-health-scan'],
    phrases: [
      'template-token cleanup',
      'library database health',
      'mbid sticking',
      'mb id gap',
      'clean orphaned',
      'clean empty album',
    ],
  },
  {
    tabId: 'album-tracks',
    types: ['repair-mb-tracks', 'duplicate-track-resolver'],
    phrases: ['album track', 'extra track', 'bad track', 'remove bad tracks', 'repair mb track'],
  },
  {
    tabId: 'artist-alias',
    phrases: ['artist alias', 'artist aliases', 'artist-id', 'artist id'],
  },
  {
    tabId: 'artist-folders',
    types: ['artist-folder-scan', 'artist-folder-merge', 'stamp-mbid-folders'],
    phrases: ['artist folder'],
  },
  {
    tabId: 'duplicates',
    types: ['dedup-scan', 'dedup-ai-review', 'merge-duplicate-album'],
    phrases: ['duplicate', 'dedup'],
  },
  {
    tabId: 'library-db',
    phrases: ['orphan', 'empty album', 'library health'],
  },
];

interface TabJobCount {
  running: number;
  failed: number;
}

interface CleanOverviewState {
  loading: boolean;
  error: string;
  running: number;
  failed: number;
  lastChecked: number | null;
  lastJob: Job | null;
  byTab: Record<string, TabJobCount>;
  lastByTab: Record<string, Job | null>;
}

function emptyTabJobCounts() {
  return Object.fromEntries(TAB_IDS.map((id) => [id, { running: 0, failed: 0 }])) as Record<string, TabJobCount>;
}

function emptyTabJobs() {
  return Object.fromEntries(TAB_IDS.map((id) => [id, null])) as Record<string, Job | null>;
}

function cleanTabIndex(tabId: string | null | undefined) {
  if (!tabId) return -1;
  return TAB_IDS.indexOf(tabId.trim().toLowerCase());
}

function storedCleanTab() {
  try {
    return window.localStorage.getItem(CLEAN_TAB_STORAGE_KEY) ?? '';
  } catch {
    return '';
  }
}

function resolveCleanTab(searchParams: URLSearchParams, hash: string): {
  index: number;
  source: CleanTabSource;
  hasExplicitTab: boolean;
} {
  const queryTab = searchParams.get('tab');
  const hashTab = hash.replace(/^#/, '');
  const hasExplicitTab = Boolean(queryTab || hashTab);

  const queryIndex = cleanTabIndex(searchParams.get('tab'));
  if (queryIndex >= 0) return { index: queryIndex, source: 'query', hasExplicitTab };

  const hashIndex = cleanTabIndex(hashTab);
  if (hashIndex >= 0) return { index: hashIndex, source: 'hash', hasExplicitTab };

  const storedIndex = cleanTabIndex(storedCleanTab());
  if (storedIndex >= 0) return { index: storedIndex, source: 'storage', hasExplicitTab };

  return { index: 0, source: hasExplicitTab ? 'fallback' : 'default', hasExplicitTab };
}

function cleanTabId(index: number) {
  return TABS[index]?.id ?? TABS[0].id;
}

function rememberCleanTab(tabId: string) {
  try {
    window.localStorage.setItem(CLEAN_TAB_STORAGE_KEY, tabId);
  } catch {
    // Ignore private browsing/storage failures; the URL still carries state.
  }
}

function jobUpdatedAt(job: Job) {
  return Number(job.finished_at || job.started_at || job.created_at || 0);
}

function formatTimestamp(raw: number | null | undefined) {
  if (!raw) return 'Never';
  const ms = raw > 1_000_000_000_000 ? raw : raw * 1000;
  return new Date(ms).toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

function isFailedLike(job: Job) {
  return job.status === 'failed' || job.status === 'killed' || job.status === 'cancelled';
}

function cleanJobTabId(job: Job) {
  const jobType = String(job.metadata?.type ?? '').toLowerCase();
  const label = `${job.label ?? ''}`.toLowerCase();
  const haystack = `${label} ${jobType}`;

  const rule = CLEAN_JOB_TAB_RULES.find(({ types, phrases }) => (
    types?.includes(jobType) || phrases?.some((phrase) => haystack.includes(phrase))
  ));
  return rule?.tabId ?? '';
}

function summarizeCleanJobs(jobs: Job[]) {
  const byTab = emptyTabJobCounts();
  const lastByTab = emptyTabJobs();
  const cleanJobs = jobs
    .filter((job) => cleanJobTabId(job))
    .sort((a, b) => jobUpdatedAt(b) - jobUpdatedAt(a));

  for (const job of cleanJobs) {
    const tabId = cleanJobTabId(job);
    if (!tabId || !byTab[tabId]) continue;
    if (job.status === 'running') byTab[tabId].running += 1;
    if (isFailedLike(job)) byTab[tabId].failed += 1;
    if (!lastByTab[tabId]) lastByTab[tabId] = job;
  }

  return {
    running: cleanJobs.filter((job) => job.status === 'running').length,
    failed: cleanJobs.filter(isFailedLike).length,
    lastJob: cleanJobs[0] ?? null,
    byTab,
    lastByTab,
  };
}

function tabBadge(counts: TabJobCount | undefined) {
  if (!counts) return null;
  if (counts.running) return { label: String(counts.running), className: 'bg-sky-500/20 text-sky-200' };
  if (counts.failed) return { label: '!', className: 'bg-rose-500/20 text-rose-200' };
  return null;
}

function tabStatusLabel(counts: TabJobCount | undefined) {
  if (!counts) return '';
  if (counts.running) return `${counts.running} active`;
  if (counts.failed) return `${counts.failed} failed`;
  return '';
}

function compactJobLabel(job: Job | null) {
  if (!job) return 'None';
  return `${job.label} · ${job.status}`;
}

function TruncatedMetricValue({ value }: { value: string }) {
  return <span className="block truncate text-sm" title={value}>{value}</span>;
}

export default function Clean({ embedded = false }: { embedded?: boolean }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const resolvedTab = useMemo(
    () => resolveCleanTab(searchParams, location.hash),
    [searchParams, location.hash],
  );
  const resolvedIndex = resolvedTab.index;
  const [selectedIndex, setSelectedIndex] = useState(embedded ? 0 : resolvedIndex);
  const [overview, setOverview] = useState<CleanOverviewState>({
    loading: false,
    error: '',
    running: 0,
    failed: 0,
    lastChecked: null,
    lastJob: null,
    byTab: emptyTabJobCounts(),
    lastByTab: emptyTabJobs(),
  });

  useEffect(() => {
    if (!embedded) setSelectedIndex(resolvedIndex);
  }, [embedded, resolvedIndex]);

  useEffect(() => {
    if (embedded) return;
    const tabId = cleanTabId(resolvedIndex);
    rememberCleanTab(tabId);

    const queryTab = searchParams.get('tab');
    const queryNeedsCanonicalId = Boolean(queryTab && queryTab !== tabId);
    const shouldCanonicalize =
      Boolean(location.hash) ||
      queryNeedsCanonicalId ||
      (resolvedTab.source !== 'query' && (resolvedTab.hasExplicitTab || resolvedTab.source === 'storage'));

    if (!shouldCanonicalize) return;

    const nextParams = new URLSearchParams(searchParams);
    nextParams.set('tab', tabId);
    navigate(
      {
        pathname: location.pathname,
        search: `?${nextParams.toString()}`,
        hash: '',
      },
      { replace: true },
    );
  }, [
    embedded,
    location.hash,
    location.pathname,
    navigate,
    resolvedIndex,
    resolvedTab.hasExplicitTab,
    resolvedTab.source,
    searchParams,
  ]);

  const loadOverview = useCallback(async (silent = false) => {
    if (!silent) {
      setOverview((current) => ({ ...current, loading: true, error: '' }));
    }
    try {
      const response = await apiGet<JobListResponse>('/api/jobs');
      const summary = summarizeCleanJobs(response.jobs ?? []);
      setOverview({
        ...summary,
        loading: false,
        error: '',
        lastChecked: Date.now(),
      });
    } catch (err) {
      setOverview((current) => ({
        ...current,
        loading: false,
        error: err instanceof Error ? err.message : String(err),
        lastChecked: Date.now(),
      }));
    }
  }, []);

  useEffect(() => {
    void loadOverview();
  }, [loadOverview]);

  useEffect(() => {
    const handleJobsChanged = () => {
      void loadOverview(true);
    };
    window.addEventListener('beets:jobs-changed', handleJobsChanged);
    return () => window.removeEventListener('beets:jobs-changed', handleJobsChanged);
  }, [loadOverview]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      if (!document.hidden) void loadOverview(true);
    }, CLEAN_OVERVIEW_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [loadOverview]);

  const handleTabChange = (index: number) => {
    const tabId = cleanTabId(index);
    setSelectedIndex(index);
    rememberCleanTab(tabId);
    if (!embedded) {
      const nextParams = new URLSearchParams(searchParams);
      nextParams.set('tab', tabId);
      setSearchParams(nextParams, { replace: true });
    }
  };

  const selectedTab = TABS[selectedIndex] ?? TABS[0];
  const selectedTabJob = overview.lastByTab[selectedTab.id] ?? null;

  return (
    <div className="space-y-4">
      {!embedded && (
        <CleanPanelHeader
          title="Clean"
          description={selectedTab.description}
          meta={(
            <>
              <span>{overview.running} active cleanup job(s)</span>
              <span>{overview.failed} failed or stopped cleanup job(s)</span>
              <span>Status checked {formatTimestamp(overview.lastChecked ? overview.lastChecked / 1000 : null)}</span>
            </>
          )}
          actions={(
            <>
              <Chip color="info" label={selectedTab.label} size="small" variant="outlined" />
              {overview.error ? <Chip color="error" label="Status error" size="small" variant="outlined" /> : null}
              <Button disabled={overview.loading} size="small" variant="outlined" onClick={() => void loadOverview()}>
                {overview.loading ? 'Checking...' : 'Refresh Status'}
              </Button>
            </>
          )}
        />
      )}

      {!embedded && overview.error ? (
        <div className="rounded-md border border-rose-900/50 bg-rose-950/15 px-3 py-2 text-sm text-rose-200">
          {overview.error}
        </div>
      ) : null}

      {!embedded && (
        <CleanMetricGrid
          items={[
            {
              label: 'Active jobs',
              value: overview.running,
              tone: overview.running ? 'info' : 'success',
            },
            {
              label: 'Failed jobs',
              value: overview.failed,
              tone: overview.failed ? 'danger' : 'success',
            },
            {
              label: `${selectedTab.label} last job`,
              value: <TruncatedMetricValue value={compactJobLabel(selectedTabJob)} />,
              tone: selectedTabJob && isFailedLike(selectedTabJob) ? 'danger' : 'neutral',
            },
            {
              label: 'Latest clean job',
              value: <TruncatedMetricValue value={compactJobLabel(overview.lastJob)} />,
              tone: overview.lastJob && isFailedLike(overview.lastJob) ? 'danger' : 'neutral',
            },
          ]}
        />
      )}

      {embedded && overview.error ? (
        <div className="rounded-md border border-rose-900/50 bg-rose-950/15 px-3 py-2 text-sm text-rose-200">
          {overview.error}
        </div>
      ) : null}

      <TabGroup className="min-w-0" selectedIndex={selectedIndex} onChange={handleTabChange}>
        <TabList className="mb-4 flex w-full min-w-0 max-w-full gap-1 overflow-x-auto rounded-md border border-graphite-800/90 bg-graphite-950/65 p-1 shadow-sm shadow-black/20">
          {TABS.map(({ id, label }) => {
            const badge = tabBadge(overview.byTab[id]);
            const statusLabel = tabStatusLabel(overview.byTab[id]);
            return (
              <Tab
                key={id}
                className={({ selected }) =>
                  [
                    'flex min-h-[2.5rem] shrink-0 flex-col items-start justify-center gap-0.5 rounded px-3.5 py-2 text-left text-[0.82rem] font-medium outline-none transition-colors',
                    selected
                      ? 'bg-red-500/15 text-zinc-100 shadow-sm shadow-black/20 ring-1 ring-red-400/40'
                      : 'text-zinc-400 hover:bg-graphite-900/70 hover:text-zinc-200',
                  ].join(' ')
                }
              >
                <span className="flex items-center gap-2">
                  <span>{label}</span>
                  {badge ? (
                    <span className={`rounded-full px-1.5 py-0.5 text-[0.65rem] font-semibold tabular-nums ${badge.className}`}>
                      {badge.label}
                    </span>
                  ) : null}
                </span>
                {statusLabel ? <span className="text-[0.67rem] font-normal text-zinc-500">{statusLabel}</span> : null}
              </Tab>
            );
          })}
        </TabList>

        <TabPanels>
          <TabPanel><LibraryHealthPanel active={selectedIndex === 0} /></TabPanel>
          <TabPanel><DedupPanel /></TabPanel>
          <TabPanel><AlbumTracksPanel /></TabPanel>
          <TabPanel><ArtistAliasPanel active={selectedIndex === 3} /></TabPanel>
          <TabPanel><ArtistFoldersPanel /></TabPanel>
        </TabPanels>
      </TabGroup>
    </div>
  );
}
