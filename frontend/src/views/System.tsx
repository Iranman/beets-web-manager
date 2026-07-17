import { Dialog, DialogBackdrop, DialogPanel, DialogTitle } from '@headlessui/react';
import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Checkbox from '@mui/material/Checkbox';
import CircularProgress from '@mui/material/CircularProgress';
import LinearProgress from '@mui/material/LinearProgress';
import TextField from '@mui/material/TextField';
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  completeSetup,
  getConfigFile,
  getSetupEnv,
  getSetupStatus,
  revertConfigFile,
  saveConfigFile,
  saveSetupEnv,
} from '../api/client';
import type {
  ConfigFileResponse,
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

export default function System() {
  const [status, setStatus] = useState<SetupStatusResponse | null>(null);
  const [env, setEnv] = useState<SetupEnvResponse | null>(null);
  const [form, setForm] = useState<Record<string, string>>({});
  const [clearNames, setClearNames] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [completing, setCompleting] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [configText, setConfigText] = useState('');
  const [savedConfigText, setSavedConfigText] = useState('');
  const [configMeta, setConfigMeta] = useState<Pick<ConfigFileResponse, 'has_backup' | 'backup_ts'> | null>(null);
  const [configLoading, setConfigLoading] = useState(true);
  const [configBusy, setConfigBusy] = useState(false);
  const [configError, setConfigError] = useState('');
  const [configMsg, setConfigMsg] = useState('');
  const [configAction, setConfigAction] = useState<ConfigAction>(null);

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

  useEffect(() => {
    void load();
    void loadConfig();
  }, [load, loadConfig]);

  const grouped = useMemo(() => groupVariables(env?.variables ?? []), [env?.variables]);
  const configDirty = configText !== savedConfigText;
  const configEmpty = !configText.trim();

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

  const refreshSystem = () => {
    void load();
    void loadConfig();
  };

  const markComplete = async () => {
    setCompleting(true);
    setError('');
    setMessage('');
    try {
      await completeSetup();
      setStatus((current) => current ? { ...current, setup_complete: true } : current);
      setMessage('System readiness marked complete.');
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
          <h1 className="text-xl font-semibold text-zinc-100">System</h1>
          <div className="mt-1 text-sm text-zinc-500">
            {status?.version ? `Version ${status.version}` : 'Environment, readiness, and Beets configuration'}
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outlined" size="small" onClick={() => refreshSystem()} disabled={loading || configLoading}>
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
          label="Readiness marker"
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
            {completing ? 'Marking...' : status?.setup_complete ? 'Complete' : 'Mark system ready'}
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

      <section className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="text-base font-semibold text-zinc-100">config.yaml</h2>
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
