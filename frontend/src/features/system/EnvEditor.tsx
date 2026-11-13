import { errorMessage, panelError } from '../../lib/errors';
import { useEngineOffline } from '../../components/EngineOfflineBanner';
import { WarningIcon } from '../../components/WarningIcon';
import { Dialog, DialogBackdrop, DialogPanel, DialogTitle } from '@headlessui/react';
import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Checkbox from '@mui/material/Checkbox';
import LinearProgress from '@mui/material/LinearProgress';
import TextField from '@mui/material/TextField';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  apiErrorBody,
  authorizeSetupEnvReveal,
  getSetupEnv,
  revealSetupEnvValue,
  saveSetupEnv,
} from '../../api/client';
import type { SetupEnvResponse, SetupEnvVariable } from '../../api/types';

// The environment editor, moved unchanged from the System page. System mounts it for every
// section except "Authentication & Security"; Settings > Security mounts it for that section
// only, so each variable has exactly one place where it is edited.
export const SECURITY_SECTION = 'Authentication & Security';

function initialFormValues(variables: SetupEnvVariable[]): Record<string, string> {
  const values: Record<string, string> = {};
  for (const variable of variables) {
    values[variable.name] = variable.secret ? '' : variable.value;
  }
  return values;
}

const CANONICAL_SECTION_ORDER = [
  'System & Environment',
  'Authentication & Security',
  'AI & LLM Services',
  'Beets Core & Engine',
  'Storage & Paths',
  'Music Services & Metadata',
  'Media Server Integrations',
  'Playlists & Download Providers',
];

function groupVariables(variables: SetupEnvVariable[]): Array<[string, SetupEnvVariable[]]> {
  const groups: Record<string, SetupEnvVariable[]> = {};
  for (const variable of variables) {
    const section = variable.section || 'System & Environment';
    if (!groups[section]) {
      groups[section] = [];
    }
    groups[section].push(variable);
  }
  const orderedSections: string[] = [];
  for (const sec of CANONICAL_SECTION_ORDER) {
    if (groups[sec]) {
      orderedSections.push(sec);
    }
  }
  for (const sec of Object.keys(groups)) {
    if (!orderedSections.includes(sec)) {
      orderedSections.push(sec);
    }
  }
  return orderedSections.map((section) => [section, groups[section]]);
}

// Browser passwords are passphrase-friendly and separate from the API bearer
// token length floor. Keep this in sync with app.py/routes_setup.py.
const PASSWORD_MIN_LENGTH = 16;
const PASSWORD_REQUIREMENTS: Array<{ label: string; test: (v: string) => boolean }> = [
  { label: `At least ${PASSWORD_MIN_LENGTH} characters`, test: (v) => v.length >= PASSWORD_MIN_LENGTH },
];

function passwordStrength(value: string): { met: number; total: number; label: string; tone: 'ok' | 'warn' | 'bad' } {
  const met = PASSWORD_REQUIREMENTS.filter((req) => req.test(value)).length;
  const total = PASSWORD_REQUIREMENTS.length;
  if (!value) return { met: 0, total, label: '', tone: 'bad' };
  if (met === total) return { met, total, label: 'Ready', tone: 'ok' };
  return { met, total, label: 'Too short', tone: 'bad' };
}

function PasswordStrengthMeter({ value }: { value: string }) {
  const strength = passwordStrength(value);
  const barColor = strength.tone === 'ok' ? 'bg-emerald-400' : strength.tone === 'warn' ? 'bg-amber-400' : 'bg-red-400';
  return (
    <div className="mt-2 space-y-1.5">
      {value && (
        <div className="flex items-center gap-2">
          <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-graphite-800">
            <div
              className={`h-full rounded-full transition-[width] motion-reduce:transition-none ${barColor}`}
              style={{ width: `${(strength.met / strength.total) * 100}%` }}
            />
          </div>
          <span className={`text-xs font-semibold ${strength.tone === 'ok' ? 'text-emerald-300' : strength.tone === 'warn' ? 'text-amber-300' : 'text-red-300'}`}>
            {strength.label}
          </span>
        </div>
      )}
      <div className="flex flex-wrap gap-1.5">
        {PASSWORD_REQUIREMENTS.map((req) => {
          const met = req.test(value);
          return (
            <span
              key={req.label}
              className={`rounded px-1.5 py-0.5 text-xs font-semibold ${
                met ? 'bg-emerald-950/45 text-emerald-300' : 'bg-graphite-800 text-muted'
              }`}
            >
              {met ? '✓' : '○'} {req.label}
            </span>
          );
        })}
      </div>
    </div>
  );
}

function sourceBadge(source: string): { label: string; className: string } {
  switch (source) {
    case 'environment':
    case 'process':
      return {
        label: 'environment',
        className: 'bg-emerald-950/70 text-emerald-300 border border-emerald-800/80',
      };
    case 'persisted':
    case 'file':
      return {
        label: 'persisted',
        className: 'bg-blue-950/70 text-blue-300 border border-blue-800/80',
      };
    case 'runtime':
      return {
        label: 'runtime',
        className: 'bg-cyan-950/70 text-cyan-300 border border-cyan-800/80',
      };
    case 'default':
    case 'example':
      return {
        label: 'default',
        className: 'bg-graphite-800 text-zinc-300 border border-graphite-700',
      };
    case 'override':
      return {
        label: 'override',
        className: 'bg-amber-950/70 text-amber-300 border border-amber-800/80',
      };
    case 'not_configured':
    default:
      return {
        label: 'not configured',
        className: 'bg-zinc-900 text-zinc-400 border border-zinc-800',
      };
  }
}

function EnvVariableRow({
  variable,
  value,
  clear,
  onValue,
  onClear,
  revealedValue,
  revealing,
  revealError,
  onShow,
  onHide,
}: {
  variable: SetupEnvVariable;
  value: string;
  clear: boolean;
  onValue: (value: string) => void;
  onClear: (checked: boolean) => void;
  revealedValue: string | undefined;
  revealing: boolean;
  revealError: string;
  onShow: () => void;
  onHide: () => void;
}) {
  const changed = variable.secret ? value !== '' || clear : value !== variable.value;
  const badge = sourceBadge(variable.source);
  const notEditable = variable.editable === false;
  const canReveal = variable.secret && variable.revealable === true && variable.configured;
  const isRevealed = revealedValue !== undefined;
  const [copied, setCopied] = useState(false);
  const [isChangingPassword, setIsChangingPassword] = useState(false);

  const isPassword = variable.name === 'BEETS_WEB_PASSWORD';

  return (
    <div className="grid gap-3 py-3 lg:grid-cols-[minmax(14rem,22rem)_minmax(0,1fr)_auto]">
      <div className="min-w-0">
        <div className="break-all text-sm font-semibold text-zinc-100">{variable.name}</div>
        <div className="mt-1.5 flex flex-wrap gap-1.5 text-xs font-semibold">
          <span className={`rounded px-1.5 py-0.5 ${badge.className}`}>{badge.label}</span>
          {variable.secret && (
            <span className="rounded bg-red-950/50 px-1.5 py-0.5 text-red-200 border border-red-800/50">secret</span>
          )}
          {variable.configured && !clear && value === '' && (
            <span className="rounded bg-emerald-950/50 px-1.5 py-0.5 text-emerald-300 border border-emerald-800/50">Configured</span>
          )}
          {!variable.configured && !clear && value === '' && (
            <span className="rounded bg-zinc-900 px-1.5 py-0.5 text-zinc-400 border border-zinc-800">Not configured</span>
          )}
          {variable.restart_required && (
            <span className="rounded bg-purple-950/50 px-1.5 py-0.5 text-purple-300 border border-purple-800/50">Restart required</span>
          )}
          {variable.is_overridden && (
            <span className="rounded bg-amber-950/60 px-1.5 py-0.5 text-amber-300 border border-amber-800/60">Overridden</span>
          )}
          {variable.secret && value !== '' && !clear && (
            <span className="rounded bg-amber-950/60 px-1.5 py-0.5 text-amber-300 border border-amber-800/60">Will replace existing value</span>
          )}
          {clear && (
            <span className="rounded bg-red-950/60 px-1.5 py-0.5 text-red-300 border border-red-800/60">Will remove configured value</span>
          )}
          {!variable.secret && changed && (
            <span className="rounded bg-amber-950/60 px-1.5 py-0.5 text-amber-300 border border-amber-800/60">changed</span>
          )}
          {notEditable && (
            <span className="rounded bg-zinc-900 px-1.5 py-0.5 text-zinc-400 border border-zinc-800">not editable here</span>
          )}
        </div>
        <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-zinc-400">
          {variable.container_path && (
            <span>
              <span className="text-muted">Container:</span> <code className="text-zinc-300">{variable.container_path}</code>
            </span>
          )}
          {variable.host_path && (
            <span>
              <span className="text-muted">Host:</span> <code className="text-zinc-300">{variable.host_path}</code>
            </span>
          )}
          {variable.default !== null && variable.default !== undefined && variable.default !== '' && (
            <span>
              <span className="text-muted">Default:</span> <code className="text-zinc-300">{variable.default}</code>
            </span>
          )}
          {variable.description && (
            <div className="w-full text-zinc-400 leading-relaxed mt-0.5">{variable.description}</div>
          )}
        </div>

        {variable.is_overridden && (
          <div className="mt-2 rounded border border-amber-800/50 bg-amber-950/30 p-2 text-xs text-amber-200">
            <div className="font-semibold text-amber-300"><WarningIcon /> Environment overrides saved configuration</div>
            <div className="mt-0.5 font-mono text-zinc-300">
              <span>Running: </span>
              <strong className="text-amber-200">{variable.effective_value || '(set in environment)'}</strong>
              {variable.saved_value && (
                <>
                  <span className="mx-1.5 text-muted">|</span>
                  <span>Saved: </span>
                  <span className="text-zinc-400">{variable.saved_value}</span>
                </>
              )}
            </div>
          </div>
        )}
      </div>

      <div className="min-w-0 flex flex-col justify-center">
        {/* Reveal display area */}
        {isRevealed && (
          <div className="mb-2 flex items-center gap-2 rounded border border-emerald-700/70 bg-emerald-950/40 p-2 text-xs">
            <span className="font-mono text-xs font-bold text-emerald-400">VALUE:</span>
            <code className="min-w-0 flex-1 break-all font-mono text-xs text-emerald-200 select-all font-semibold">
              {revealedValue}
            </code>
            <Button
              size="small"
              variant="outlined"
              onClick={() => {
                void navigator.clipboard?.writeText(revealedValue).then(() => setCopied(true)).catch(() => undefined);
              }}
            >
              {copied ? '✓ Copied' : 'Copy'}
            </Button>
            <Button size="small" variant="text" onClick={onHide}>
              Hide
            </Button>
          </div>
        )}

        {revealError && !isRevealed && (
          <div className="mb-1.5 text-xs text-red-300">{revealError}</div>
        )}

        {/* 1. BEETS_WEB_PASSWORD Special Row */}
        {isPassword ? (
          <div>
            {!isChangingPassword && value === '' && !clear ? (
              <div className="flex flex-wrap items-center justify-between gap-2 rounded border border-graphite-800 bg-graphite-900/60 p-2 text-xs text-zinc-300">
                <span className="text-zinc-400">
                  {variable.configured ? 'Stored securely as a one-way password hash.' : 'No browser password configured.'}
                </span>
                <Button
                  size="small"
                  variant="outlined"
                  onClick={() => setIsChangingPassword(true)}
                >
                  {variable.configured ? 'Change Password' : 'Set Password'}
                </Button>
              </div>
            ) : (
              <div className="space-y-2">
                <TextField
                  fullWidth
                  autoFocus
                  size="small"
                  type="password"
                  value={value}
                  placeholder="Enter new password (min 8 chars, mixed case/numbers)"
                  onChange={(event) => onValue(event.target.value)}
                />
                {value !== '' && <PasswordStrengthMeter value={value} />}
                <div className="flex justify-end">
                  <Button
                    size="small"
                    variant="text"
                    onClick={() => {
                      onValue('');
                      setIsChangingPassword(false);
                    }}
                  >
                    Cancel
                  </Button>
                </div>
              </div>
            )}
          </div>
        ) : (
          /* 2. Generic Input Row */
          <div className="flex items-center gap-2">
            <TextField
              fullWidth
              disabled={clear || notEditable}
              size="small"
              type={variable.secret ? 'password' : 'text'}
              value={clear ? '' : value}
              placeholder={
                notEditable
                  ? 'Set on the host via .env / docker-compose.yml'
                  : clear
                  ? 'Will be removed on save'
                  : variable.secret
                  ? variable.configured
                    ? '••••••••••••••••'
                    : 'Not configured (enter new value)'
                  : variable.default
                  ? `Default: ${variable.default}`
                  : 'Not configured'
              }
              onChange={(event) => onValue(event.target.value)}
            />
            {canReveal && !clear && value === '' && (
              <Button
                size="small"
                variant="outlined"
                onClick={() => (isRevealed ? onHide() : onShow())}
                disabled={revealing}
                sx={{ minWidth: '5.5rem', whiteSpace: 'nowrap', height: '36px' }}
              >
                {revealing ? 'Revealing…' : isRevealed ? '🙈 Hide' : '👁 Show'}
              </Button>
            )}
          </div>
        )}
      </div>

      <label className="flex items-center justify-end gap-1.5 text-xs font-semibold text-zinc-400">
        <Checkbox
          size="small"
          disabled={notEditable || (!variable.secret && !variable.configured && !variable.value) || (!variable.configured && value === '')}
          checked={clear}
          onChange={(event) => {
            onClear(event.target.checked);
            if (event.target.checked && isChangingPassword) {
              setIsChangingPassword(false);
              onValue('');
            }
          }}
        />
        Clear
      </label>
    </div>
  );
}

export function EnvEditor({
  include,
  saveLabel = 'Save environment',
  onSaved,
}: {
  /** Which env sections this editor shows and saves. Pass a stable function. */
  include: (section: string) => boolean;
  saveLabel?: string;
  onSaved?: () => void;
}) {
  const engineOffline = useEngineOffline();
  const [env, setEnv] = useState<SetupEnvResponse | null>(null);
  const [form, setForm] = useState<Record<string, string>>({});
  const [clearNames, setClearNames] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');

  // Per-field secret reveal state -- deliberately separate from `form`
  // (the edit/replacement buffer) so viewing a value can never be confused
  // with typing a replacement for it. Kept only in this component's
  // ephemeral React state: never written to localStorage/sessionStorage,
  // never part of the payload sent to saveEnv().
  const [revealedSecrets, setRevealedSecrets] = useState<Record<string, string>>({});
  const [revealingNames, setRevealingNames] = useState<Set<string>>(new Set());
  const [revealErrors, setRevealErrors] = useState<Record<string, string>>({});
  const [reauthDialog, setReauthDialog] = useState<{
    open: boolean;
    pendingName: string | null;
    password: string;
    error: string;
    submitting: boolean;
  }>({ open: false, pendingName: null, password: '', error: '', submitting: false });
  const revealTimers = useRef<Map<string, number>>(new Map());

  const hideSecret = useCallback((name: string) => {
    setRevealedSecrets((current) => {
      if (!(name in current)) return current;
      const next = { ...current };
      delete next[name];
      return next;
    });
    const timerId = revealTimers.current.get(name);
    if (timerId !== undefined) {
      window.clearTimeout(timerId);
      revealTimers.current.delete(name);
    }
  }, []);

  const hideAllSecrets = useCallback(() => {
    revealTimers.current.forEach((id) => window.clearTimeout(id));
    revealTimers.current.clear();
    setRevealedSecrets({});
  }, []);

  // Hide any revealed secret automatically on unmount (navigating away).
  useEffect(() => {
    return () => {
      revealTimers.current.forEach((id) => window.clearTimeout(id));
      revealTimers.current.clear();
    };
  }, []);

  // Hide all revealed secrets when the tab/page becomes inactive.
  useEffect(() => {
    const onVisibilityChange = () => {
      if (document.hidden) hideAllSecrets();
    };
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => document.removeEventListener('visibilitychange', onVisibilityChange);
  }, [hideAllSecrets]);

  const REVEAL_AUTO_HIDE_MS = 45_000;

  const performReveal = useCallback(async (name: string) => {
    setRevealingNames((current) => new Set(current).add(name));
    setRevealErrors((current) => {
      if (!(name in current)) return current;
      const next = { ...current };
      delete next[name];
      return next;
    });
    try {
      const result = await revealSetupEnvValue(name);
      if (result.ok && typeof result.value === 'string') {
        setRevealedSecrets((current) => ({ ...current, [name]: result.value as string }));
        const existingTimer = revealTimers.current.get(name);
        if (existingTimer !== undefined) window.clearTimeout(existingTimer);
        const timerId = window.setTimeout(() => hideSecret(name), REVEAL_AUTO_HIDE_MS);
        revealTimers.current.set(name, timerId);
      } else {
        setRevealErrors((current) => ({ ...current, [name]: result.error || 'Could not reveal value.' }));
      }
    } catch (err) {
      const body = apiErrorBody(err);
      if (body?.reauth_required) {
        setReauthDialog({ open: true, pendingName: name, password: '', error: '', submitting: false });
      } else {
        setRevealErrors((current) => ({ ...current, [name]: errorMessage(err) }));
      }
    } finally {
      setRevealingNames((current) => {
        if (!current.has(name)) return current;
        const next = new Set(current);
        next.delete(name);
        return next;
      });
    }
  }, [hideSecret]);

  const handleShowSecret = (name: string) => {
    void performReveal(name);
  };

  const submitReauth = async () => {
    const pendingName = reauthDialog.pendingName;
    if (!pendingName) return;
    setReauthDialog((current) => ({ ...current, submitting: true, error: '' }));
    try {
      const result = await authorizeSetupEnvReveal(reauthDialog.password);
      if (result.ok) {
        setReauthDialog({ open: false, pendingName: null, password: '', error: '', submitting: false });
        await performReveal(pendingName);
      } else {
        setReauthDialog((current) => ({ ...current, submitting: false, error: result.error || 'Incorrect password.' }));
      }
    } catch (err) {
      setReauthDialog((current) => ({ ...current, submitting: false, error: errorMessage(err) }));
    }
  };

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const nextEnv = await getSetupEnv();
      setEnv(nextEnv);
      setForm(initialFormValues(nextEnv.variables));
      setClearNames(new Set());
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const variablesHere = useMemo(
    () => (env?.variables ?? []).filter((variable) => include(variable.section || 'System & Environment')),
    [env?.variables, include],
  );
  const grouped = useMemo(() => groupVariables(variablesHere), [variablesHere]);

  const dirty = useMemo(() => {
    if (!env) return false;
    return variablesHere.some((variable) => {
      if (clearNames.has(variable.name)) return true;
      const nextValue = form[variable.name] ?? '';
      return variable.secret ? nextValue !== '' : nextValue !== variable.value;
    });
  }, [clearNames, env, form, variablesHere]);

  const saveEnv = async () => {
    if (!env || !dirty) return;
    setSaving(true);
    setError('');
    setMessage('');
    hideAllSecrets();
    const variables: Record<string, string> = {};
    for (const variable of variablesHere) {
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
      onSaved?.();
      const saved = updated.saved?.length ? updated.saved.join(', ') : 'environment';
      setMessage(`Saved ${saved}. Restart or recreate the container for Docker-managed values.`);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0 break-all text-xs text-muted">
          {env?.env_file ?? 'No env file'} {env && !env.exists ? '(will be created)' : ''}
        </div>
        <Button variant="contained" size="small" disabled={saving || !dirty} onClick={() => void saveEnv()}>
          {saving ? 'Saving...' : saveLabel}
        </Button>
      </div>
      {panelError(error, engineOffline) && <Alert severity="error">{error}</Alert>}
      {message && <Alert severity="success">{message}</Alert>}
      {loading && <LinearProgress sx={{ borderRadius: 1 }} />}
      {!loading && env && grouped.length === 0 && (
        <p className="m-0 text-sm text-muted">The server reported no variables for this group.</p>
      )}

      {grouped.map(([section, variables]) => (
        <div key={section}>
          <h3 className="m-0 mb-1 text-xs font-semibold text-muted">{section}</h3>
          <div className="divide-y divide-graphite-800 border-y border-graphite-800">
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
              revealedValue={revealedSecrets[variable.name]}
              revealing={revealingNames.has(variable.name)}
              revealError={revealErrors[variable.name] ?? ''}
              onShow={() => handleShowSecret(variable.name)}
              onHide={() => hideSecret(variable.name)}
            />
          ))}
          </div>
        </div>
      ))}

      <Dialog
        open={reauthDialog.open}
        onClose={() => setReauthDialog({ open: false, pendingName: null, password: '', error: '', submitting: false })}
        className="relative z-50"
      >
        <DialogBackdrop className="fixed inset-0 bg-graphite-950/60" />
        <div className="fixed inset-0 flex items-center justify-center p-4">
          <DialogPanel className="w-full max-w-sm rounded-lg border border-graphite-700 bg-graphite-900 p-5 shadow-2xl">
            <DialogTitle className="text-base font-semibold text-zinc-100">Confirm your password</DialogTitle>
            <p className="mt-2 text-sm text-zinc-400">
              Revealing a configured secret requires re-entering your administrator password. This confirmation
              covers additional reveals for a short time.
            </p>
            <TextField
              autoFocus
              fullWidth
              size="small"
              type="password"
              className="mt-3"
              label="Current password"
              value={reauthDialog.password}
              onChange={(event) => setReauthDialog((current) => ({ ...current, password: event.target.value, error: '' }))}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && reauthDialog.password && !reauthDialog.submitting) void submitReauth();
              }}
            />
            {reauthDialog.error && <div className="mt-2 text-xs text-red-300">{reauthDialog.error}</div>}
            <div className="mt-5 flex justify-end gap-2">
              <Button
                variant="outlined"
                size="small"
                onClick={() => setReauthDialog({ open: false, pendingName: null, password: '', error: '', submitting: false })}
              >
                Cancel
              </Button>
              <Button
                variant="contained"
                size="small"
                disabled={!reauthDialog.password || reauthDialog.submitting}
                onClick={() => void submitReauth()}
              >
                {reauthDialog.submitting ? 'Confirming…' : 'Confirm'}
              </Button>
            </div>
          </DialogPanel>
        </div>
      </Dialog>
    </div>
  );
}
