import { errorMessage } from '../../lib/errors';
import { Disclosure, DisclosureButton, DisclosurePanel } from '@headlessui/react';
import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router';
import {
  getPlexStatus,
  refreshPlex,
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
            <span className="text-xs text-muted">{open ? 'Hide' : 'Show'}</span>
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
      setStatusMsg(errorMessage(err));
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
      setError(errorMessage(err));
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

// ── PluginsPanel ──────────────────────────────────────────────────────────────

export function PluginsPanel() {
  const [pluginStatus, setPluginStatus] = useState<PluginStatusResponse | null>(null);
  const [installLog, setInstallLog] = useState<string[]>([]);
  const [statusError, setStatusError] = useState('');
  const unavailable = [
    { name: 'ytimport' as const, label: 'ytimport' },
    { name: 'wlg' as const, label: 'wlg' },
    { name: 'aisauce' as const, label: 'aisauce' },
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
      setStatusError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    void loadPluginStatus();
  }, [loadPluginStatus]);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="m-0 text-sm font-semibold text-zinc-100">Plugin Controls</h3>
        <Button size="small" variant="outlined" onClick={() => void loadPluginStatus()}>
          Refresh status
        </Button>
      </div>
      {statusError && <Alert severity="error">{statusError}</Alert>}
      {installLog.length > 0 ? (
        <LogViewer className="max-h-24 text-xs text-muted" text={installLog.join('\n')} />
      ) : null}
      <PlexPlugin />
      {/* Genre tagging (lastgenre) and missing art (fetchart) run from Jobs & Cleanup, their one home. */}
      <p className="text-xs text-muted">
        Genre tagging and album art are run from <Link className="text-brass-300 underline" to="/jobs">Jobs &amp; Cleanup</Link>.
      </p>
      {/* ytimport, wlg, aisauce and ytupdate had Run buttons here that called POST /api/plugins/run,
          which SEC-002 removed (no generic beet-command endpoint). They are gone until each gets its
          own job endpoint; a missing install still shows below. */}
      <MissingPluginSummary items={unavailable} />
    </div>
  );
}
