import Alert from '@mui/material/Alert';
import LinearProgress from '@mui/material/LinearProgress';
import { useCallback, useEffect, useState } from 'react';
import { getConfigOverview } from '../../api/client';
import { errorMessage } from '../../lib/errors';
import type { ConfigOverviewSetting } from '../../api/types';

// Settings > Configuration Overview (#753 D1): a read-only table of where each
// setting comes from (effective source/layer) and whether a change needs a
// restart. No edit controls — the password/username change flow is the Security
// section. Values are redacted server-side: secrets are masked and host/
// container paths are withheld, so this panel never renders a plaintext secret.

const BADGE = 'rounded px-1.5 py-0.5 border';

function sourceBadge(source: string): { label: string; className: string } {
  switch (source) {
    case 'environment':
    case 'process':
      return { label: 'Environment', className: 'bg-emerald-950/70 text-emerald-300 border-emerald-800/80' };
    case 'persisted':
    case 'file':
      return { label: 'Saved settings', className: 'bg-blue-950/70 text-blue-300 border-blue-800/80' };
    case 'runtime':
      return { label: 'Runtime', className: 'bg-cyan-950/70 text-cyan-300 border-cyan-800/80' };
    case 'default':
    case 'example':
      return { label: 'Built-in default', className: 'bg-graphite-800 text-zinc-300 border-graphite-700' };
    default:
      return { label: 'Not set', className: 'bg-zinc-900 text-zinc-400 border-zinc-800' };
  }
}

const LAYER_LABEL: Record<string, string> = {
  app: 'App setting',
  secret: 'Secret',
  host: 'Host (Compose)',
  deployment: 'Deployment (Compose)',
  container: 'Container path',
  unknown: 'Advanced (env)',
};

function layerLabel(layer: string): string {
  return LAYER_LABEL[layer] ?? layer;
}

/** The redacted value status cell. Never renders a plaintext secret (the server
 * masks them); host/container-layer values arrive as null and show presence. */
function ValueStatus({ row }: { row: ConfigOverviewSetting }) {
  if (row.missing) {
    return <span className="text-warning">Not set</span>;
  }
  if (row.secret) {
    return <span className="font-mono text-zinc-400">{row.value || 'Set'}</span>;
  }
  if (row.value === null) {
    return <span className="text-zinc-300">Set (managed outside the app)</span>;
  }
  return <span className="break-all font-mono text-zinc-200">{row.value || 'Set'}</span>;
}

export function ConfigOverview() {
  const [settings, setSettings] = useState<ConfigOverviewSetting[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const res = await getConfigOverview();
      setSettings(res.settings ?? []);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (error) {
    return <Alert severity="error">{error}</Alert>;
  }
  if (loading && settings === null) {
    return <LinearProgress aria-label="Loading configuration overview" sx={{ borderRadius: 1 }} />;
  }
  if (!settings || settings.length === 0) {
    return <p className="m-0 text-sm text-muted">No configuration settings to show.</p>;
  }

  return (
    <div className="crate min-w-0 overflow-x-auto rounded-sm">
      <table className="w-full min-w-[40rem] border-collapse text-sm" aria-label="Configuration overview">
        <thead>
          <tr className="border-b border-graphite-800 text-left text-xs uppercase tracking-wide text-muted">
            <th scope="col" className="px-3 py-2 font-semibold">Setting</th>
            <th scope="col" className="px-3 py-2 font-semibold">Source</th>
            <th scope="col" className="px-3 py-2 font-semibold">Layer</th>
            <th scope="col" className="px-3 py-2 font-semibold">Value</th>
            <th scope="col" className="px-3 py-2 font-semibold">Change</th>
          </tr>
        </thead>
        <tbody>
          {settings.map((row) => {
            const badge = sourceBadge(row.source);
            return (
              <tr
                key={row.name}
                data-testid={`config-row-${row.name}`}
                data-missing={row.missing ? 'true' : undefined}
                className={`border-b border-graphite-900 align-top ${row.missing ? 'bg-amber-950/10' : ''}`}
              >
                <th scope="row" className="px-3 py-2 text-left font-normal">
                  <div className="break-all font-mono font-semibold text-zinc-100">{row.name}</div>
                  <div className="mt-1 flex flex-wrap gap-1.5 text-xs font-semibold">
                    {row.secret && (
                      <span className={`${BADGE} bg-red-950/50 text-red-200 border-red-800/50`}>Secret</span>
                    )}
                    {row.operator && (
                      <span className={`${BADGE} bg-amber-950/60 text-amber-300 border-amber-800/60`}>Operator only</span>
                    )}
                  </div>
                </th>
                <td className="px-3 py-2">
                  <span className={`${BADGE} text-xs font-semibold ${badge.className}`}>{badge.label}</span>
                </td>
                <td className="px-3 py-2 text-zinc-300">{layerLabel(row.layer)}</td>
                <td className="px-3 py-2"><ValueStatus row={row} /></td>
                <td className="px-3 py-2">
                  {row.restart_required ? (
                    <span className={`${BADGE} text-xs font-semibold bg-purple-950/50 text-purple-300 border-purple-800/50`}>
                      {row.apply === 'deploy' ? 'Recreate container' : 'Restart required'}
                    </span>
                  ) : (
                    <span className="text-xs text-muted">Applies live</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
