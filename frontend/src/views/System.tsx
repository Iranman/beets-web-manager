import { errorMessage, panelError } from '../lib/errors';
import { useEngineOffline } from '../components/EngineOfflineBanner';
import { WarningIcon } from '../components/WarningIcon';
import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import CircularProgress from '@mui/material/CircularProgress';
import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { Link } from 'react-router';
import {
  completeSetup,
  getSetupStatus,
  testSetupAcoustid,
  testSetupAi,
  testSetupMusicBrainz,
  testSetupPlex,
  verifyPlugins,
} from '../api/client';
import type { BeetsPluginsReport, SetupIntegrationTestResponse, SetupStatusResponse } from '../api/types';
import { EnvEditor, SECURITY_SECTION } from '../features/system/EnvEditor';

// System is the diagnostics page: one health line, then integrations and paths as rows, then
// reference material behind disclosures. Editing Beets' config.yaml and sign-in settings lives in
// Settings (config.yaml, Security); the remaining environment variables are edited here, folded away.

type IntegrationKey = 'ai' | 'musicbrainz' | 'acoustid' | 'plex';

const INTEGRATION_TESTS: Record<IntegrationKey, () => Promise<SetupIntegrationTestResponse>> = {
  ai: testSetupAi,
  musicbrainz: testSetupMusicBrainz,
  acoustid: testSetupAcoustid,
  plex: testSetupPlex,
};

const SERVICE_NAMES: Record<string, string> = { ai: 'AI', musicbrainz: 'MusicBrainz', acoustid: 'AcoustID', slskd: 'slskd' };

type IntegrationStatus = SetupStatusResponse['integrations'][string];

/** "Version 0.4.0 (abc1234)": the revision is shortened to 7 characters and
 * left out when the image was not stamped with one. */
export function versionLabel(version: string, revision?: string | null): string {
  const rev = (revision ?? '').trim();
  const short = rev && rev !== 'unknown' ? (/^[0-9a-f]{8,}$/i.test(rev) ? rev.slice(0, 7) : rev) : '';
  return `Version ${version}${short ? ` (${short})` : ''}`;
}

function integrationBadge(
  integration: IntegrationStatus,
  test: SetupIntegrationTestResponse | undefined,
  testing: boolean,
): { icon: ReactNode; label: string; tone: 'ok' | 'warn' | 'neutral' } {
  if (testing) return { icon: '…', label: 'testing', tone: 'neutral' };
  if (test) {
    if (test.status === 'ready') return { icon: '✓', label: 'Connected', tone: 'ok' };
    if (test.status === 'not_configured') return { icon: '✗', label: 'Not configured', tone: integration.required ? 'warn' : 'neutral' };
    if (test.reason === 'outbound_not_allowlisted') return { icon: <WarningIcon />, label: 'Blocked by outbound policy', tone: 'warn' };
    return { icon: <WarningIcon />, label: 'Connection test failed', tone: 'warn' };
  }
  const state = integration.state || (integration.configured ? 'configured' : 'not_configured');
  if (state === 'connected') return { icon: '✓', label: 'Connected', tone: 'ok' };
  if (state === 'configured') return { icon: '✓', label: 'Configured', tone: 'ok' };
  if (state === 'installed_but_disabled') return { icon: '○', label: 'Installed but disabled', tone: integration.required ? 'warn' : 'neutral' };
  if (state === 'dependency_plugin_missing') return { icon: <WarningIcon />, label: 'Dependency/plugin missing', tone: 'warn' };
  if (state === 'plugin_loader_failed') return { icon: <WarningIcon />, label: 'Plugin loader failed', tone: 'warn' };
  if (state === 'connection_test_failed') return { icon: <WarningIcon />, label: 'Connection test failed', tone: 'warn' };
  if (state === 'unavailable') return { icon: <WarningIcon />, label: 'Beets engine unreachable', tone: integration.required ? 'warn' : 'neutral' };
  return { icon: '✗', label: 'Not configured', tone: integration.required ? 'warn' : 'neutral' };
}

const TONE_TEXT = { ok: 'text-emerald-300', warn: 'text-amber-300', neutral: 'text-zinc-300' } as const;

/** Reflects the backend-reported plugin-loader probe state only (never
 * recomputed client-side from configured_plugins/plugin_failures) --
 * `beets.available`/`plugin_loader_timed_out`/`plugin_loader_ok` are the
 * single sources of truth from /api/setup/status. */
function pluginLoaderLabel(beets: NonNullable<SetupStatusResponse['beets']>): string {
  if (!beets.available) return 'unavailable';
  if (beets.plugin_loader_timed_out) return 'timed out';
  if (beets.plugin_loader_ok) return 'loaded';
  return 'failed';
}

/** The health line's problem list. Readiness is the backend's call: its blocking reasons come
 * first, word for word. Two things it does not block on are added: a required integration that is
 * not working, and required Beets plugins that report unhealthy. */
export function healthProblems(
  status: SetupStatusResponse,
  plugins: BeetsPluginsReport | null,
  tests: Partial<Record<IntegrationKey, SetupIntegrationTestResponse>> = {},
): string[] {
  const problems = [...(status.blocking_reasons ?? [])];
  for (const [name, integration] of Object.entries(status.integrations ?? {})) {
    if (!integration.required) continue;
    const badge = integrationBadge(integration, tests[name as IntegrationKey], false);
    if (badge.tone === 'warn') problems.push(`${SERVICE_NAMES[name] ?? name}: ${badge.label}.`);
  }
  if (plugins && !plugins.all_required_healthy) {
    const unhealthy = plugins.required_count - plugins.required_healthy_count;
    problems.push(`${unhealthy} of ${plugins.required_count} required Beets plugins report a problem. Open Beets plugins below.`);
  }
  return problems;
}

function Row({ name, state, tone, children }: { name: ReactNode; state: ReactNode; tone: keyof typeof TONE_TEXT; children?: ReactNode }) {
  return (
    <li className="grid grid-cols-[minmax(0,1fr)_auto] items-baseline gap-x-4 gap-y-0.5 px-3 py-2 sm:grid-cols-[9rem_minmax(0,1fr)_auto]">
      <span className="min-w-0 truncate text-sm font-medium text-zinc-100">{name}</span>
      {/* State precedes the detail in the DOM so text and screen readers get "name, state, detail"; grid placement keeps the visual order. */}
      <span className={`col-start-2 row-start-1 flex items-center gap-1 justify-self-end whitespace-nowrap text-xs font-semibold sm:col-start-3 ${TONE_TEXT[tone]}`}>{state}</span>
      <span className="col-span-2 col-start-1 row-start-2 min-w-0 text-xs text-zinc-300 sm:col-span-1 sm:row-start-1 sm:col-start-2">{children}</span>
    </li>
  );
}

function Disclosure({ title, hint, children }: { title: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <details className="group py-1">
      <summary className="flex min-h-11 cursor-pointer list-none flex-wrap items-baseline gap-x-3 gap-y-0.5 rounded-sm py-2 [&::-webkit-details-marker]:hidden">
        <span aria-hidden="true" className="inline-block w-3 text-brass-300 transition-transform group-open:rotate-90 motion-reduce:transition-none">›</span>
        <span className="font-display text-base font-semibold text-zinc-100">{title}</span>
        {hint && <span className="min-w-0 text-xs text-muted">{hint}</span>}
      </summary>
      <div className="pb-4 pl-6">{children}</div>
    </details>
  );
}

const notSecurity = (section: string) => section !== SECURITY_SECTION;

export default function System() {
  const engineOffline = useEngineOffline();
  const [status, setStatus] = useState<SetupStatusResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [completing, setCompleting] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [envKey, setEnvKey] = useState(0);
  const [integrationTests, setIntegrationTests] = useState<Partial<Record<IntegrationKey, SetupIntegrationTestResponse>>>({});
  const [integrationTesting, setIntegrationTesting] = useState<Partial<Record<IntegrationKey, boolean>>>({});
  const [pluginsReport, setPluginsReport] = useState<BeetsPluginsReport | null>(null);
  const [pluginsLoading, setPluginsLoading] = useState(false);
  const [pluginsMsg, setPluginsMsg] = useState('');
  const [pluginsError, setPluginsError] = useState('');

  const loadStatus = useCallback(async () => {
    try {
      const nextStatus = await getSetupStatus();
      setStatus(nextStatus);
      if (nextStatus.plugins) setPluginsReport(nextStatus.plugins);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    await loadStatus();
    setLoading(false);
  }, [loadStatus]);

  useEffect(() => {
    void load();
  }, [load]);

  const refreshSystem = () => {
    void load();
    setEnvKey((key) => key + 1);
  };

  const runIntegrationTests = useCallback(() => {
    (Object.keys(INTEGRATION_TESTS) as IntegrationKey[]).forEach((key) => {
      setIntegrationTesting((current) => ({ ...current, [key]: true }));
      // Each integration is tested independently: one provider timing out or
      // failing must never block or hide the result of the others.
      INTEGRATION_TESTS[key]()
        .then((result) => {
          setIntegrationTests((current) => ({ ...current, [key]: result }));
        })
        .catch((err) => {
          setIntegrationTests((current) => ({
            ...current,
            [key]: { ok: false, status: 'failed', error: errorMessage(err) },
          }));
        })
        .finally(() => {
          setIntegrationTesting((current) => ({ ...current, [key]: false }));
        });
    });
  }, []);

  const handleVerifyPlugins = async () => {
    setPluginsLoading(true);
    setPluginsMsg('');
    setPluginsError('');
    try {
      const res = await verifyPlugins();
      setPluginsReport(res);
      setPluginsMsg(res.ok ? 'All required Beets plugins verified healthy.' : 'Plugin verification found issues.');
    } catch (err) {
      setPluginsError(errorMessage(err));
    } finally {
      setPluginsLoading(false);
    }
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
      setError(errorMessage(err));
    } finally {
      setCompleting(false);
    }
  };

  if (loading && !status) {
    return (
      <div className="flex min-h-[24rem] items-center justify-center">
        <CircularProgress size={28} aria-label="Loading system status" />
      </div>
    );
  }

  const testingAny = Object.values(integrationTesting).some(Boolean);
  const problems = status ? healthProblems(status, pluginsReport, integrationTests) : [];
  const integrations = Object.entries(status?.integrations ?? {});
  const services = integrations.filter(([, integration]) => integration.category !== 'beets_plugin');
  const pluginIntegrations = integrations.filter(([, integration]) => integration.category === 'beets_plugin');
  const beets = status?.beets;
  const paths = status
    ? [
        { label: 'Config', path: status.paths.config.path, ok: status.paths.config.writable === true, bad: 'not writable' },
        { label: 'Music library', path: status.paths.music_library.path, ok: status.paths.music_library.exists, bad: 'missing' },
        { label: 'Downloads', path: status.paths.downloads.path, ok: status.paths.downloads.writable === true, bad: 'not writable' },
        { label: 'Beets config', path: status.paths.beets_config.path, ok: status.paths.beets_config.exists, bad: 'missing' },
      ]
    : [];

  return (
    <div className="min-w-0 space-y-6 overflow-x-clip">
      <header className="flex flex-wrap items-start justify-between gap-3 border-b border-graphite-800 pb-4">
        <div className="min-w-0">
          <h1 className="lib-title">System</h1>
          <p className="lib-catalog">
            <span>{status?.version ? versionLabel(status.version, status.revision) : 'Version unknown'}</span>
            <span>Beets {beets?.version || (beets?.available ? 'available' : 'unavailable')}</span>
            {status?.demo_mode ? <span>Demo mode</span> : null}
            {status ? (
              <span role="status" className={`lib-review ${problems.length ? 'is-flag' : 'is-ok'}`}>
                {problems.length ? `${problems.length} ${problems.length === 1 ? 'problem' : 'problems'}` : 'All systems OK'}
              </span>
            ) : null}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          {status && !status.setup_complete ? (
            <Button variant="outlined" size="small" disabled={completing} onClick={() => void markComplete()}>
              {completing ? 'Marking...' : 'Mark system ready'}
            </Button>
          ) : null}
          <Button variant="outlined" size="small" onClick={refreshSystem} disabled={loading}>
            Refresh
          </Button>
        </div>
      </header>

      {panelError(error, engineOffline) && <Alert severity="error">{`System status could not be loaded: ${error}`}</Alert>}
      {message && <Alert severity="success">{message}</Alert>}

      {problems.length > 0 && (
        <ul aria-label="Problems" className="m-0 list-none space-y-1 p-0">
          {problems.map((problem, index) => (
            <li key={index} className="flex gap-2 text-sm text-zinc-100">
              <span className="shrink-0 font-semibold text-amber-300">Problem</span>
              <span className="min-w-0 break-words">{problem}</span>
            </li>
          ))}
        </ul>
      )}

      {status && (
        <div className="grid gap-x-8 gap-y-6 lg:grid-cols-2">
          <section aria-labelledby="system-integrations" className="min-w-0 space-y-2">
            <div className="flex min-h-9 flex-wrap items-center justify-between gap-2">
              <h2 id="system-integrations" className="m-0 font-display text-lg font-semibold text-zinc-100">Integrations</h2>
              <Button variant="outlined" size="small" onClick={runIntegrationTests} disabled={testingAny}>
                {testingAny ? 'Testing...' : 'Test connections'}
              </Button>
            </div>
            <ul className="crate m-0 list-none divide-y divide-walnut-950 rounded-sm p-0">
              {services.map(([name, integration]) => {
                const key = name as IntegrationKey;
                const test = integrationTests[key];
                const badge = integrationBadge(integration, test, Boolean(integrationTesting[key]));
                const reason = test?.error || (!test && (integration.detail || integration.note)) || (integration.required ? 'Required' : '');
                return (
                  <Row
                    key={name}
                    name={<span className="capitalize">{SERVICE_NAMES[name] ?? name}</span>}
                    tone={badge.tone}
                    state={<><span aria-hidden="true">{badge.icon}</span>{badge.label}</>}
                  >
                    {reason}
                  </Row>
                );
              })}
            </ul>
            <p className="m-0 text-xs text-muted">Each integration is tested on its own; one failing does not stop the others.</p>
          </section>

          <section aria-labelledby="system-paths" className="min-w-0 space-y-2">
            <h2 id="system-paths" className="m-0 flex min-h-9 items-center font-display text-lg font-semibold text-zinc-100">Paths</h2>
            <ul className="crate m-0 list-none divide-y divide-walnut-950 rounded-sm p-0">
              {paths.map((row) => (
                <Row key={row.label} name={row.label} tone={row.ok ? 'ok' : 'warn'} state={row.ok ? 'ok' : row.bad}>
                  <code className="block truncate text-zinc-300" title={row.path}>{row.path || 'not configured'}</code>
                </Row>
              ))}
              <Row name="fpcalc" tone={status.fpcalc.available ? 'ok' : 'warn'} state={status.fpcalc.available ? 'ok' : 'missing'}>
                <code className="block truncate text-zinc-300" title={status.fpcalc.path}>{status.fpcalc.path || 'not found on PATH'}</code>
              </Row>
            </ul>
          </section>
        </div>
      )}

      <section aria-label="Reference" className="divide-y divide-graphite-800 border-y border-graphite-800">
        {beets && (
          <Disclosure
            title="Beets plugins"
            hint={`${beets.configured_plugins?.length ?? 0} enabled · loader ${pluginLoaderLabel(beets)}${
              pluginsReport ? ` · ${pluginsReport.required_healthy_count} of ${pluginsReport.required_count} required healthy` : ''
            }`}
          >
            <div className="space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <p className="m-0 text-xs text-muted">
                  Read-only. Plugin actions live in <Link className="text-brass-300 underline underline-offset-2" to="/config#plugins">Settings › Plugins</Link>.
                </p>
                <Button variant="outlined" size="small" onClick={() => void handleVerifyPlugins()} disabled={pluginsLoading}>
                  {pluginsLoading ? 'Checking...' : 'Recheck plugins'}
                </Button>
              </div>
              {pluginsMsg && <Alert severity="success">{pluginsMsg}</Alert>}
              {panelError(pluginsError, engineOffline) && <Alert severity="error">{pluginsError}</Alert>}
              <dl className="m-0 grid gap-x-6 gap-y-1 text-xs text-zinc-300 sm:grid-cols-2">
                <div><dt className="inline font-semibold text-zinc-100">Plugin path:</dt> <dd className="inline m-0">{beets.pluginpath?.join(' then ') || 'not configured'}</dd></div>
                <div><dt className="inline font-semibold text-zinc-100">ReplayGain:</dt> <dd className="inline m-0">{beets.replaygain_backend || beets.replaygain_command || 'not configured'}</dd></div>
                <div><dt className="inline font-semibold text-zinc-100">Enabled plugins:</dt> <dd className="inline m-0">{beets.configured_plugins?.length ?? 0}</dd></div>
                <div>
                  <dt className="inline font-semibold text-zinc-100">Plugin loader:</dt>{' '}
                  <dd className={`inline m-0 font-semibold ${pluginLoaderLabel(beets) === 'loaded' ? 'text-emerald-300' : 'text-red-300'}`}>{pluginLoaderLabel(beets)}</dd>
                </div>
              </dl>
              {beets.plugin_failures?.length ? (
                <Alert severity="warning">{beets.plugin_failures.join(' ')}</Alert>
              ) : beets.plugin_loader_error ? (
                <Alert severity="warning">{beets.plugin_loader_error}</Alert>
              ) : null}
              {pluginsReport && (
                <div className="grid gap-x-8 gap-y-3 lg:grid-cols-2">
                  <div className="min-w-0">
                    <h3 className="m-0 mb-1 text-xs font-semibold text-zinc-300">
                      Required core plugins ({pluginsReport.required_healthy_count} of {pluginsReport.required_count} healthy)
                    </h3>
                    <ul className="crate m-0 list-none divide-y divide-walnut-950 rounded-sm p-0">
                      {pluginsReport.categories.required.map((plugin) => (
                        <Row key={plugin.name} name={plugin.display_name || plugin.name} tone={plugin.healthy ? 'ok' : 'warn'} state={plugin.healthy ? '✓ Ready' : <><WarningIcon /> Issue</>}>
                          {plugin.description}
                        </Row>
                      ))}
                    </ul>
                  </div>
                  <div className="min-w-0">
                    <h3 className="m-0 mb-1 text-xs font-semibold text-zinc-300">Optional & integration plugins</h3>
                    <ul className="crate m-0 list-none divide-y divide-walnut-950 rounded-sm p-0">
                      {[...pluginsReport.categories.optional, ...pluginsReport.categories.integration].map((plugin) => (
                        <Row key={plugin.name} name={plugin.display_name || plugin.name} tone={plugin.enabled ? 'ok' : 'neutral'} state={plugin.enabled ? '✓ Enabled' : '○ Available'}>
                          {plugin.description}
                        </Row>
                      ))}
                    </ul>
                  </div>
                </div>
              )}
              {pluginIntegrations.length > 0 && (
                <div className="min-w-0">
                  <h3 className="m-0 mb-1 text-xs font-semibold text-zinc-300">Plugins used by Web Manager features</h3>
                  <ul className="crate m-0 list-none divide-y divide-walnut-950 rounded-sm p-0">
                    {pluginIntegrations.map(([name, integration]) => {
                      const badge = integrationBadge(integration, undefined, false);
                      return (
                        <Row key={name} name={name} tone={badge.tone} state={<><span aria-hidden="true">{badge.icon}</span>{badge.label}</>}>
                          {integration.detail || integration.note}
                        </Row>
                      );
                    })}
                  </ul>
                </div>
              )}
            </div>
          </Disclosure>
        )}

        <Disclosure title="Environment variables" hint="Values Web Manager runs with. Secrets stay masked.">
          <p className="m-0 mb-3 text-xs text-muted">
            Sign-in, API token and outbound policy variables are edited in{' '}
            <Link className="text-brass-300 underline underline-offset-2" to="/config#security">Settings › Security</Link>.
          </p>
          <EnvEditor key={envKey} include={notSecurity} onSaved={() => void loadStatus()} />
        </Disclosure>

        {status && (
          <Disclosure title="Beets config" hint={status.paths.beets_config.path || 'not configured'}>
            <dl className="m-0 grid gap-y-1 text-xs text-zinc-300">
              <div><dt className="inline font-semibold text-zinc-100">Config file:</dt> <dd className="inline m-0 break-all"><code>{status.paths.beets_config.path || 'not configured'}</code> {status.paths.beets_config.exists ? '(found)' : '(missing)'}</dd></div>
              {beets?.path ? <div><dt className="inline font-semibold text-zinc-100">Beets:</dt> <dd className="inline m-0 break-all"><code>{beets.path}</code></dd></div> : null}
              {beets?.engine_compatibility?.message ? <div><dt className="inline font-semibold text-zinc-100">Compatibility:</dt> <dd className="inline m-0">{beets.engine_compatibility.message}</dd></div> : null}
              <div><dt className="inline font-semibold text-zinc-100">Readiness marker:</dt> <dd className="inline m-0">{status.setup_complete ? 'complete' : 'open'}</dd></div>
            </dl>
            <p className="m-0 mt-3 text-xs text-muted">
              Edit, reload or revert config.yaml in{' '}
              <Link className="text-brass-300 underline underline-offset-2" to="/config#config-yaml">Settings › config.yaml</Link>.
            </p>
          </Disclosure>
        )}
      </section>
    </div>
  );
}
