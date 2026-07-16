import { Disclosure, DisclosureButton, DisclosurePanel } from '@headlessui/react';
import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import TextField from '@mui/material/TextField';
import { useCallback, useEffect, useState } from 'react';
import {
  fetchMissingArt,
  fixGenres,
  getPlexStatus,
  refreshPlex,
  runPlugin,
  getPluginInstallLog,
  getPluginStatus,
} from '../../api/client';
import type { PluginCommandName, PluginCommandStatus, PluginStatusResponse } from '../../api/types';
import { LogViewer } from '../../components/LogViewer';
import { useJobPoll } from '../../lib/hooks';

function PluginSection({
  title,
  badges,
  children,
}: {
  title: string;
  badges?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <Disclosure>
      {({ open }) => (
        <div className="rounded-md border border-graphite-800 bg-graphite-950/40">
          <DisclosureButton className="flex w-full items-center justify-between px-4 py-3 text-left text-sm font-semibold text-zinc-200 hover:bg-graphite-900/50">
            <span className="flex items-center gap-2">
              {title}
              {badges}
            </span>
            <span className="text-xs text-zinc-500">{open ? 'Hide' : 'Show'}</span>
          </DisclosureButton>
          <DisclosurePanel className="space-y-3 border-t border-graphite-800 p-3">
            {children}
          </DisclosurePanel>
        </div>
      )}
    </Disclosure>
  );
}

function JobLog({ job }: { job: { status: string; log?: string[] } | null }) {
  if (!job) return null;
  const log = (job.log ?? []).join('\n');
  const color =
    job.status === 'success' ? 'text-emerald-400' :
    job.status === 'failed' || job.status === 'killed' ? 'text-red-400' :
    'text-zinc-400';
  if (!log) return null;
  return (
    <LogViewer className={`max-h-48 text-xs ${color}`} text={log} />
  );
}

function statusChip(status: string | undefined) {
  if (status === 'running') return <Chip color="warning" label="Running" size="small" variant="outlined" />;
  if (status === 'success') return <Chip color="success" label="Done" size="small" variant="outlined" />;
  if (status === 'failed' || status === 'killed') return <Chip color="error" label="Failed" size="small" variant="outlined" />;
  return null;
}

function commandStatus(
  payload: PluginStatusResponse | null,
  name: PluginCommandName,
): PluginCommandStatus | undefined {
  const detailed = payload?.status?.[name];
  if (detailed) return detailed;
  const installed = payload?.installed?.[name];
  if (typeof installed !== 'boolean') return undefined;
  return {
    installed,
    enabled: Boolean(payload?.enabled?.[name]),
    runnable: installed,
  };
}

function PluginBadges({ status }: { status?: PluginCommandStatus }) {
  if (!status) {
    return <Chip label="Checking" size="small" variant="outlined" />;
  }
  return (
    <span className="flex flex-wrap items-center gap-1">
      <Chip
        color={status.runnable ? 'success' : 'error'}
        label={status.runnable ? 'Runnable' : 'Missing'}
        size="small"
        variant="outlined"
      />
      <Chip
        color={status.enabled ? 'success' : 'default'}
        label={status.enabled ? 'Config enabled' : 'Temp config'}
        size="small"
        variant="outlined"
      />
    </span>
  );
}

function PluginUnavailable({ status }: { status?: PluginCommandStatus }) {
  if (!status || status.runnable) return null;
  return <p className="text-xs text-red-400">Plugin is not installed in this app environment.</p>;
}

function MissingPluginSummary({ items }: { items: Array<{ name: PluginCommandName; label: string }> }) {
  if (!items.length) return null;
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-md border border-graphite-800 bg-graphite-950/40 px-3 py-2 text-xs text-zinc-300">
      <span className="font-medium text-zinc-200">Unavailable</span>
      {items.map((item) => (
        <Chip key={item.name} color="error" label={item.label} size="small" variant="outlined" />
      ))}
    </div>
  );
}

// ── Plex ──────────────────────────────────────────────────────────────────────

function PlexPlugin() {
  const [statusMsg, setStatusMsg] = useState('');
  const [statusColor, setStatusColor] = useState<'success' | 'error' | ''>('');
  const [refreshJobId, setRefreshJobId] = useState<string | null>(null);
  const [error, setError] = useState('');
  const { job } = useJobPoll(refreshJobId);

  async function checkStatus() {
    setStatusMsg('Checking...');
    setStatusColor('');
    try {
      const s = await getPlexStatus();
      if (s.connected) {
        setStatusMsg(`Connected — "${s.section_title}" at ${s.url}`);
        setStatusColor('success');
      } else {
        setStatusMsg(s.error || 'Not connected');
        setStatusColor('error');
      }
    } catch (err) {
      setStatusMsg(err instanceof Error ? err.message : String(err));
      setStatusColor('error');
    }
  }

  async function handleRefresh() {
    setError('');
    setRefreshJobId(null);
    try {
      const { job_id } = await refreshPlex();
      setRefreshJobId(job_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <PluginSection title="Plex Direct">
      <div className="flex flex-wrap items-center gap-2">
        <Button size="small" variant="outlined" onClick={() => void checkStatus()}>Check Status</Button>
        <Button disabled={job?.status === 'running'} size="small" variant="outlined" onClick={() => void handleRefresh()}>
          Refresh Library
        </Button>
        {statusChip(job?.status)}
        {error && <span className="text-xs text-red-400">{error}</span>}
      </div>
      {statusMsg && (
        <p className={`text-xs ${statusColor === 'success' ? 'text-emerald-400' : statusColor === 'error' ? 'text-red-400' : 'text-zinc-400'}`}>
          {statusMsg}
        </p>
      )}
      <JobLog job={job} />
    </PluginSection>
  );
}

// ── Core Beets maintenance ────────────────────────────────────────────────────

function LastgenrePlugin() {
  const [jobId, setJobId] = useState<string | null>(null);
  const [error, setError] = useState('');
  const { job } = useJobPoll(jobId);
  const busy = job?.status === 'running';

  async function run(opts: { force?: boolean; useAi?: boolean }) {
    setError('');
    setJobId(null);
    try {
      const { job_id } = await fixGenres(opts);
      setJobId(job_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <PluginSection
      title="Genre Tagging (lastgenre)"
      badges={<Chip color="success" label="Config enabled" size="small" variant="outlined" />}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Button disabled={busy} size="small" variant="outlined" onClick={() => void run({})}>
          Tag Missing
        </Button>
        <Button disabled={busy} size="small" variant="outlined" onClick={() => void run({ useAi: true })}>
          Tag Missing + AI
        </Button>
        <Button color="warning" disabled={busy} size="small" variant="outlined" onClick={() => void run({ force: true })}>
          Force Re-tag All
        </Button>
        {statusChip(job?.status)}
        {error && <span className="text-xs text-red-400">{error}</span>}
      </div>
      <JobLog job={job} />
    </PluginSection>
  );
}

function FetchartPlugin() {
  const [jobId, setJobId] = useState<string | null>(null);
  const [error, setError] = useState('');
  const { job } = useJobPoll(jobId);

  async function run() {
    setError('');
    setJobId(null);
    try {
      const { job_id } = await fetchMissingArt();
      setJobId(job_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <PluginSection
      title="Album Art (fetchart)"
      badges={<Chip color="success" label="Config enabled" size="small" variant="outlined" />}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Button disabled={job?.status === 'running'} size="small" variant="outlined" onClick={() => void run()}>
          Fetch Missing Art
        </Button>
        {statusChip(job?.status)}
        {error && <span className="text-xs text-red-400">{error}</span>}
      </div>
      <JobLog job={job} />
    </PluginSection>
  );
}

// ── YouTube Import (ytimport) ─────────────────────────────────────────────────

function YtImportPlugin({ pluginStatus }: { pluginStatus?: PluginCommandStatus }) {
  const [url, setUrl] = useState('');
  const [jobId, setJobId] = useState<string | null>(null);
  const [error, setError] = useState('');
  const { job } = useJobPoll(jobId);

  async function run(args: string[], label: string) {
    setError('');
    setJobId(null);
    try {
      const { job_id } = await runPlugin(args, label);
      setJobId(job_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  const busy = job?.status === 'running';
  const locked = busy || !pluginStatus?.runnable;

  return (
    <PluginSection title="YouTube Import (ytimport)" badges={<PluginBadges status={pluginStatus} />}>
      <PluginUnavailable status={pluginStatus} />
      <div className="flex flex-wrap gap-2">
        <TextField
          className="w-full sm:max-w-sm"
          disabled={locked}
          label="YouTube URL"
          placeholder="https://youtube.com/watch?v=…"
          size="small"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
        />
        <Button
          disabled={locked || !url.trim()}
          size="small"
          variant="outlined"
          onClick={() => void run(['ytimport', '--no-likes', url.trim()], `YT: import URL`)}
        >
          Import URL
        </Button>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button
          disabled={locked}
          size="small"
          variant="outlined"
          onClick={() => void run(['ytimport', '--likes', '--max-likes', '50'], 'YT: import 50 liked songs')}
        >
          Import 50 Liked Songs
        </Button>
        {statusChip(job?.status)}
        {error && <span className="text-xs text-red-400">{error}</span>}
      </div>
      <p className="text-xs text-zinc-500">Requires <code>/config/ytmusicapi.json</code> (OAuth or headers from ytmusicapi).</p>
      <JobLog job={job} />
    </PluginSection>
  );
}

// ── Genre Tagging (wlg) ───────────────────────────────────────────────────────

function WlgPlugin({ pluginStatus }: { pluginStatus?: PluginCommandStatus }) {
  const [query, setQuery] = useState('');
  const [jobId, setJobId] = useState<string | null>(null);
  const [error, setError] = useState('');
  const { job } = useJobPoll(jobId);

  async function run(args: string[], label: string) {
    setError('');
    setJobId(null);
    try {
      const { job_id } = await runPlugin(args, label);
      setJobId(job_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  const busy = job?.status === 'running';
  const locked = busy || !pluginStatus?.runnable;

  return (
    <PluginSection title="Genre Tagging (wlg)" badges={<PluginBadges status={pluginStatus} />}>
      <PluginUnavailable status={pluginStatus} />
      <div className="flex flex-wrap gap-2">
        <TextField
          className="w-full sm:max-w-sm"
          disabled={locked}
          label="Beets query (blank = all)"
          placeholder="artist:Radiohead"
          size="small"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <Button
          disabled={locked}
          size="small"
          variant="outlined"
          onClick={() => {
            const args = query.trim() ? ['wlg', query.trim()] : ['wlg'];
            const label = query.trim() ? `wlg: fetch genres [${query.trim()}]` : 'wlg: fetch genres';
            void run(args, label);
          }}
        >
          Run
        </Button>
        <Button
          color="warning"
          disabled={locked}
          size="small"
          variant="outlined"
          onClick={() => void run(['wlg', '-f'], 'wlg: force re-tag all genres')}
        >
          Force Re-tag All
        </Button>
        {statusChip(job?.status)}
        {error && <span className="text-xs text-red-400">{error}</span>}
      </div>
      <JobLog job={job} />
    </PluginSection>
  );
}

// ── AI Metadata (aisauce) ─────────────────────────────────────────────────────

function AisaucePlugin({ pluginStatus }: { pluginStatus?: PluginCommandStatus }) {
  const [query, setQuery] = useState('');
  const [jobId, setJobId] = useState<string | null>(null);
  const [error, setError] = useState('');
  const { job } = useJobPoll(jobId);

  async function run() {
    setError('');
    setJobId(null);
    const args = query.trim() ? ['aisauce', query.trim()] : ['aisauce'];
    const label = query.trim() ? `aisauce: clean metadata [${query.trim()}]` : 'aisauce: clean metadata';
    try {
      const { job_id } = await runPlugin(args, label);
      setJobId(job_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  const busy = job?.status === 'running';
  const locked = busy || !pluginStatus?.runnable;

  return (
    <PluginSection title="AI Metadata (aisauce)" badges={<PluginBadges status={pluginStatus} />}>
      <PluginUnavailable status={pluginStatus} />
      <Alert severity="info" sx={{ py: 0, fontSize: '0.75rem' }}>
        Set <code>aisauce.providers[0].api_key</code> in config.yaml before use.
      </Alert>
      <div className="flex flex-wrap gap-2">
        <TextField
          className="w-full sm:max-w-sm"
          disabled={locked}
          label="Beets query (blank = all)"
          placeholder="album:Californication"
          size="small"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <Button disabled={locked} size="small" variant="outlined" onClick={() => void run()}>
          Run
        </Button>
        {statusChip(job?.status)}
        {error && <span className="text-xs text-red-400">{error}</span>}
      </div>
      <JobLog job={job} />
    </PluginSection>
  );
}

// ── YouTube View Counts (ytupdate) ────────────────────────────────────────────

function YtUpdatePlugin({ pluginStatus }: { pluginStatus?: PluginCommandStatus }) {
  const [jobId, setJobId] = useState<string | null>(null);
  const [error, setError] = useState('');
  const { job } = useJobPoll(jobId);

  async function run() {
    setError('');
    setJobId(null);
    try {
      const { job_id } = await runPlugin(['ytupdate'], 'ytupdate: sync view counts');
      setJobId(job_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  const locked = job?.status === 'running' || !pluginStatus?.runnable;

  return (
    <PluginSection title="YouTube View Counts (ytupdate)" badges={<PluginBadges status={pluginStatus} />}>
      <PluginUnavailable status={pluginStatus} />
      <p className="text-xs text-zinc-500">
        Requires <code>/config/oauth.json</code> from ytmusicapi OAuth.
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <Button disabled={locked} size="small" variant="outlined" onClick={() => void run()}>
          Update View Counts
        </Button>
        {statusChip(job?.status)}
        {error && <span className="text-xs text-red-400">{error}</span>}
      </div>
      <JobLog job={job} />
    </PluginSection>
  );
}

// ── PluginsPanel ──────────────────────────────────────────────────────────────

export function PluginsPanel() {
  const [pluginStatus, setPluginStatus] = useState<PluginStatusResponse | null>(null);
  const [installLog, setInstallLog] = useState<string[]>([]);
  const [statusError, setStatusError] = useState('');
  const ytimportStatus = commandStatus(pluginStatus, 'ytimport');
  const wlgStatus = commandStatus(pluginStatus, 'wlg');
  const aisauceStatus = commandStatus(pluginStatus, 'aisauce');
  const ytupdateStatus = commandStatus(pluginStatus, 'ytupdate');
  const unavailable = [
    { name: 'wlg' as const, label: 'wlg' },
    { name: 'ytupdate' as const, label: 'ytupdate' },
  ].filter((item) => {
    const status = commandStatus(pluginStatus, item.name);
    return status && !status.runnable;
  });

  const loadPluginStatus = useCallback(async () => {
    setStatusError('');
    try {
      const [status, log] = await Promise.all([getPluginStatus(), getPluginInstallLog()]);
      setPluginStatus(status);
      setInstallLog(log.log ?? []);
    } catch (err) {
      setStatusError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    void loadPluginStatus();
  }, [loadPluginStatus]);

  return (
    <section className="space-y-2 rounded-md border border-graphite-800 bg-graphite-950/50 p-3">
      <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-zinc-200">Plugin Controls</h2>
        <Button size="small" variant="outlined" onClick={() => void loadPluginStatus()}>
          Refresh status
        </Button>
      </div>
      {statusError && <Alert severity="error">{statusError}</Alert>}
      {installLog.length > 0 ? (
        <LogViewer className="max-h-24 text-xs text-zinc-500" text={installLog.join('\n')} />
      ) : null}
      <PlexPlugin />
      <LastgenrePlugin />
      <FetchartPlugin />
      {ytimportStatus?.runnable ? <YtImportPlugin pluginStatus={ytimportStatus} /> : null}
      {aisauceStatus?.runnable ? <AisaucePlugin pluginStatus={aisauceStatus} /> : null}
      {wlgStatus?.runnable ? <WlgPlugin pluginStatus={wlgStatus} /> : null}
      {ytupdateStatus?.runnable ? <YtUpdatePlugin pluginStatus={ytupdateStatus} /> : null}
      <MissingPluginSummary items={unavailable} />
    </section>
  );
}
