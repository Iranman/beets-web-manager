import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Checkbox from '@mui/material/Checkbox';
import CircularProgress from '@mui/material/CircularProgress';
import TextField from '@mui/material/TextField';
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  completeSetup,
  getSetupEnv,
  getSetupStatus,
  saveSetupEnv,
} from '../api/client';
import type {
  SetupEnvResponse,
  SetupEnvVariable,
  SetupStatusResponse,
} from '../api/types';

function initialFormValues(variables: SetupEnvVariable[]): Record<string, string> {
  const values: Record<string, string> = {};
  for (const variable of variables) {
    values[variable.name] = variable.secret ? '' : variable.value;
  }
  return values;
}

function groupVariables(variables: SetupEnvVariable[]): Array<[string, SetupEnvVariable[]]> {
  const order: string[] = [];
  const groups: Record<string, SetupEnvVariable[]> = {};
  for (const variable of variables) {
    const section = variable.section || 'General';
    if (!groups[section]) {
      groups[section] = [];
      order.push(section);
    }
    groups[section].push(variable);
  }
  return order.map((section) => [section, groups[section]]);
}

function sourceLabel(source: string): string {
  if (source === 'file') return 'env file';
  if (source === 'process') return 'runtime';
  if (source === 'example') return 'default';
  return source;
}

function StatusCard({
  label,
  value,
  tone = 'neutral',
}: {
  label: string;
  value: string;
  tone?: 'neutral' | 'ok' | 'warn';
}) {
  const color = tone === 'ok' ? 'text-emerald-300' : tone === 'warn' ? 'text-amber-300' : 'text-zinc-100';
  return (
    <div className="rounded border border-graphite-800 bg-graphite-900 px-4 py-3">
      <div className="text-[0.68rem] font-semibold uppercase tracking-wide text-zinc-500">{label}</div>
      <div className={`mt-1 text-sm font-semibold ${color}`}>{value}</div>
    </div>
  );
}

function PathRow({ label, path, ok }: { label: string; path: string; ok: boolean }) {
  return (
    <div className="flex min-w-0 items-center justify-between gap-3 border-t border-graphite-800 py-2 first:border-t-0">
      <div className="min-w-0">
        <div className="text-[0.78rem] font-medium text-zinc-300">{label}</div>
        <div className="truncate text-[0.72rem] text-zinc-500">{path || 'not configured'}</div>
      </div>
      <span className={`shrink-0 text-[0.72rem] font-semibold ${ok ? 'text-emerald-300' : 'text-red-300'}`}>
        {ok ? 'ok' : 'check'}
      </span>
    </div>
  );
}

function EnvVariableRow({
  variable,
  value,
  clear,
  onValue,
  onClear,
}: {
  variable: SetupEnvVariable;
  value: string;
  clear: boolean;
  onValue: (value: string) => void;
  onClear: (checked: boolean) => void;
}) {
  const changed = variable.secret ? value !== '' || clear : value !== variable.value;
  return (
    <div className="grid gap-3 rounded border border-graphite-800 bg-graphite-950/35 p-3 lg:grid-cols-[minmax(12rem,18rem)_minmax(0,1fr)_auto]">
      <div className="min-w-0">
        <div className="break-all text-[0.78rem] font-semibold text-zinc-200">{variable.name}</div>
        <div className="mt-1 flex flex-wrap gap-1.5 text-[0.68rem] font-semibold">
          <span className="rounded bg-graphite-800 px-1.5 py-0.5 text-zinc-400">{sourceLabel(variable.source)}</span>
          {variable.secret && <span className="rounded bg-red-950/45 px-1.5 py-0.5 text-red-200">secret</span>}
          {variable.runtime_has_value && <span className="rounded bg-emerald-950/45 px-1.5 py-0.5 text-emerald-300">runtime set</span>}
          {changed && <span className="rounded bg-amber-950/55 px-1.5 py-0.5 text-amber-300">changed</span>}
        </div>
      </div>

      <TextField
        fullWidth
        disabled={clear}
        size="small"
        type={variable.secret ? 'password' : 'text'}
        value={clear ? '' : value}
        placeholder={variable.secret && variable.has_value ? variable.value : ''}
        onChange={(event) => onValue(event.target.value)}
      />

      <label className="flex items-center justify-end gap-1.5 text-[0.72rem] font-semibold text-zinc-400">
        <Checkbox
          size="small"
          disabled={!variable.secret && !variable.has_value}
          checked={clear}
          onChange={(event) => onClear(event.target.checked)}
        />
        Clear
      </label>
    </div>
  );
}

export default function Setup() {
  const [status, setStatus] = useState<SetupStatusResponse | null>(null);
  const [env, setEnv] = useState<SetupEnvResponse | null>(null);
  const [form, setForm] = useState<Record<string, string>>({});
  const [clearNames, setClearNames] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [completing, setCompleting] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [nextStatus, nextEnv] = await Promise.all([getSetupStatus(), getSetupEnv()]);
      setStatus(nextStatus);
      setEnv(nextEnv);
      setForm(initialFormValues(nextEnv.variables));
      setClearNames(new Set());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const grouped = useMemo(() => groupVariables(env?.variables ?? []), [env?.variables]);

  const dirty = useMemo(() => {
    if (!env) return false;
    return env.variables.some((variable) => {
      if (clearNames.has(variable.name)) return true;
      const nextValue = form[variable.name] ?? '';
      return variable.secret ? nextValue !== '' : nextValue !== variable.value;
    });
  }, [clearNames, env, form]);

  const saveEnv = async () => {
    if (!env || !dirty) return;
    setSaving(true);
    setError('');
    setMessage('');
    const variables: Record<string, string> = {};
    for (const variable of env.variables) {
      const nextValue = form[variable.name] ?? '';
      if (clearNames.has(variable.name)) {
        variables[variable.name] = '';
      } else if (variable.secret) {
        if (nextValue !== '') variables[variable.name] = nextValue;
      } else if (nextValue !== variable.value) {
        variables[variable.name] = nextValue;
      }
    }
    try {
      const updated = await saveSetupEnv({ variables, clear: Array.from(clearNames) });
      setEnv(updated);
      setForm(initialFormValues(updated.variables));
      setClearNames(new Set());
      const saved = updated.saved?.length ? updated.saved.join(', ') : 'environment';
      setMessage(`Saved ${saved}. Restart or recreate the container for Docker-managed values.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const markComplete = async () => {
    setCompleting(true);
    setError('');
    setMessage('');
    try {
      await completeSetup();
      setStatus((current) => current ? { ...current, setup_complete: true } : current);
      setMessage('Setup marked complete.');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setCompleting(false);
    }
  };

  if (loading) {
    return (
      <div className="flex min-h-[24rem] items-center justify-center">
        <CircularProgress size={28} />
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <section className="flex flex-wrap items-start justify-between gap-3 border-b border-graphite-800 pb-4">
        <div>
          <h1 className="text-xl font-semibold text-zinc-100">Setup</h1>
          <div className="mt-1 text-sm text-zinc-500">
            {status?.version ? `Version ${status.version}` : 'Environment and readiness'}
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outlined" size="small" onClick={() => void load()}>
            Refresh
          </Button>
          <Button
            variant="contained"
            size="small"
            disabled={saving || !dirty}
            onClick={() => void saveEnv()}
          >
            {saving ? 'Saving...' : 'Save environment'}
          </Button>
        </div>
      </section>

      {error && <Alert severity="error">{error}</Alert>}
      {message && <Alert severity="success">{message}</Alert>}

      <section className="grid gap-3 md:grid-cols-4">
        <StatusCard
          label="Readiness"
          value={status?.status ?? 'unknown'}
          tone={status?.status === 'ready' ? 'ok' : 'warn'}
        />
        <StatusCard
          label="Setup marker"
          value={status?.setup_complete ? 'complete' : 'open'}
          tone={status?.setup_complete ? 'ok' : 'warn'}
        />
        <StatusCard
          label="Demo mode"
          value={status?.demo_mode ? 'enabled' : 'off'}
          tone={status?.demo_mode ? 'warn' : 'neutral'}
        />
        <StatusCard
          label="fpcalc"
          value={status?.fpcalc.available ? 'available' : 'missing'}
          tone={status?.fpcalc.available ? 'ok' : 'warn'}
        />
      </section>

      {status?.blocking_reasons?.length ? (
        <Alert severity="warning">
          {status.blocking_reasons.join(' ')}
        </Alert>
      ) : null}

      {status && (
        <section className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(20rem,24rem)]">
          <div className="rounded border border-graphite-800 bg-graphite-900 p-4">
            <div className="mb-2 text-sm font-semibold text-zinc-100">Paths</div>
            <PathRow label="Config" path={status.paths.config.path} ok={status.paths.config.writable === true} />
            <PathRow label="Music library" path={status.paths.music_library.path} ok={status.paths.music_library.exists} />
            <PathRow label="Downloads" path={status.paths.downloads.path} ok={status.paths.downloads.writable === true} />
            <PathRow label="Beets config" path={status.paths.beets_config.path} ok={status.paths.beets_config.exists} />
          </div>
          <div className="rounded border border-graphite-800 bg-graphite-900 p-4">
            <div className="mb-2 text-sm font-semibold text-zinc-100">Integrations</div>
            {Object.entries(status.integrations).map(([name, integration]) => (
              <div key={name} className="flex items-center justify-between gap-3 border-t border-graphite-800 py-2 first:border-t-0">
                <span className="text-[0.78rem] font-medium capitalize text-zinc-300">{name}</span>
                <span className={`text-[0.72rem] font-semibold ${integration.configured ? 'text-emerald-300' : integration.required ? 'text-red-300' : 'text-zinc-500'}`}>
                  {integration.configured ? 'configured' : integration.required ? 'required' : 'not set'}
                </span>
              </div>
            ))}
          </div>
        </section>
      )}

      <section className="space-y-3">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 className="text-base font-semibold text-zinc-100">Environment variables</h2>
            <div className="mt-1 break-all text-[0.72rem] text-zinc-500">
              {env?.env_file ?? 'No env file'} {env?.exists ? '' : '(will be created)'}
            </div>
          </div>
          <Button
            variant="outlined"
            size="small"
            disabled={completing || status?.setup_complete}
            onClick={() => void markComplete()}
          >
            {completing ? 'Marking...' : status?.setup_complete ? 'Complete' : 'Mark setup complete'}
          </Button>
        </div>

        {grouped.map(([section, variables]) => (
          <div key={section} className="space-y-2">
            <div className="text-[0.72rem] font-semibold uppercase tracking-wide text-zinc-500">{section}</div>
            {variables.map((variable) => (
              <EnvVariableRow
                key={variable.name}
                variable={variable}
                value={form[variable.name] ?? ''}
                clear={clearNames.has(variable.name)}
                onValue={(nextValue) => {
                  setForm((current) => ({ ...current, [variable.name]: nextValue }));
                  if (clearNames.has(variable.name)) {
                    setClearNames((current) => {
                      const next = new Set(current);
                      next.delete(variable.name);
                      return next;
                    });
                  }
                }}
                onClear={(checked) => {
                  setClearNames((current) => {
                    const next = new Set(current);
                    if (checked) next.add(variable.name);
                    else next.delete(variable.name);
                    return next;
                  });
                }}
              />
            ))}
          </div>
        ))}
      </section>
    </div>
  );
}
