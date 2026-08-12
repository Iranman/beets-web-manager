import { useEffect, useState } from 'react';
import {
  completeSetup,
  getSetupStatus,
  saveSetupEnv,
  submitFirstRunSetup,
  testSetupAcoustid,
  testSetupAi,
  testSetupBeets,
  testSetupMusicBrainz,
  testSetupPlex,
} from '../api/client';
import type { SetupStatusResponse } from '../api/types';

function BeetsLogo() {
  return (
    <span className="flex size-12 items-center justify-center rounded-xl bg-red-700 text-white shadow-lg shadow-red-950/50 ring-1 ring-red-400/40">
      <svg className="size-7" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M12 7.4c3.5 0 6.1 2.9 5.5 6.3-.5 3.4-2.8 6.2-5.5 6.2s-5-2.8-5.5-6.2c-.6-3.4 2-6.3 5.5-6.3Z" fill="currentColor" />
        <path d="M12 7.9c-.2-2.5.8-4.1 3-4.8.3 2.3-.8 3.9-3 4.8Z" fill="#fca5a5" />
        <path d="M11.8 7.9c-2.4-.2-3.9-1.3-4.6-3.2 2.3-.1 3.9.9 4.6 3.2Z" fill="#fecaca" />
        <path d="M12 7.7V4.4" stroke="#fee2e2" strokeWidth="1.4" strokeLinecap="round" />
      </svg>
    </span>
  );
}

export default function FirstRunSetup() {
  const [currentStep, setCurrentStep] = useState<number>(1);
  const [setupStatus, setSetupStatus] = useState<SetupStatusResponse | null>(null);

  // Admin credentials state
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);

  // Connection testing states
  const [beetsStatus, setBeetsStatus] = useState<{ testing: boolean; ok?: boolean; message?: string; error?: string }>({ testing: false });
  const [showBeetsDetails, setShowBeetsDetails] = useState(false);

  // MusicBrainz state
  const [mbStatus, setMbStatus] = useState<{ testing: boolean; ok?: boolean; message?: string; error?: string }>({ testing: false });

  // AcoustID state
  const [acoustidKey, setAcoustidKey] = useState('');
  const [acoustidStatus, setAcoustidStatus] = useState<{ testing: boolean; ok?: boolean; message?: string; error?: string }>({ testing: false });
  const [acoustidSkipped, setAcoustidSkipped] = useState(false);

  // AI state
  const [aiApiKey, setAiApiKey] = useState('');
  const [aiBaseUrl, setAiBaseUrl] = useState('https://api.openai.com/v1');
  const [aiModel, setAiModel] = useState('gpt-4o-mini');
  const [aiStatus, setAiStatus] = useState<{ testing: boolean; ok?: boolean; message?: string; error?: string }>({ testing: false });
  const [aiSkipped, setAiSkipped] = useState(false);

  // Plex state
  const [plexUrl, setPlexUrl] = useState('');
  const [plexToken, setPlexToken] = useState('');
  const [plexStatus, setPlexStatus] = useState<{ testing: boolean; ok?: boolean; message?: string; error?: string }>({ testing: false });
  const [plexSkipped, setPlexSkipped] = useState(false);

  // Wizard execution state
  const [finishing, setFinishing] = useState(false);
  const [finishError, setFinishError] = useState<string | null>(null);
  const [finishSuccess, setFinishSuccess] = useState(false);

  useEffect(() => {
    getSetupStatus().then((status) => {
      setSetupStatus(status);
      if (status.beets?.version) {
        setBeetsStatus({ testing: false, ok: true, message: `Connected — Beets ${status.beets.version}` });
      }
    }).catch(() => {
      // Ignore
    });
  }, []);

  // Password requirements calculation
  const minLength = 16;
  const hasLength = password.length >= minLength;
  const passwordsMatch = password !== '' && password === confirmPassword;
  const adminFormValid = username.trim() !== '' && passwordsMatch && hasLength;

  // Actions
  const handleTestBeets = async () => {
    setBeetsStatus({ testing: true });
    try {
      const res = await testSetupBeets();
      if (res.ok) {
        setBeetsStatus({ testing: false, ok: true, message: res.message || `Connected — Beets ${res.beets_version || res.version}` });
      } else {
        setBeetsStatus({ testing: false, ok: false, error: res.error || 'Could not execute Beets. Check the Docker service and configuration.' });
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Could not reach Beets engine.';
      setBeetsStatus({ testing: false, ok: false, error: msg });
    }
  };

  const handleTestMB = async () => {
    setMbStatus({ testing: true });
    try {
      const res = await testSetupMusicBrainz();
      if (res.ok) {
        setMbStatus({ testing: false, ok: true, message: 'MusicBrainz connection successful.' });
      } else {
        setMbStatus({ testing: false, ok: false, error: res.error || 'Could not reach MusicBrainz.' });
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'MusicBrainz test failed.';
      setMbStatus({ testing: false, ok: false, error: msg });
    }
  };

  const handleTestAcoustid = async () => {
    setAcoustidStatus({ testing: true });
    try {
      const res = await testSetupAcoustid({ api_key: acoustidKey });
      if (res.ok) {
        setAcoustidStatus({ testing: false, ok: true, message: 'AcoustID ready.' });
      } else {
        setAcoustidStatus({ testing: false, ok: false, error: res.error || 'AcoustID check failed.' });
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'AcoustID test failed.';
      setAcoustidStatus({ testing: false, ok: false, error: msg });
    }
  };

  const handleTestAi = async () => {
    setAiStatus({ testing: true });
    try {
      const res = await testSetupAi({ api_key: aiApiKey, base_url: aiBaseUrl, model: aiModel });
      if (res.ok) {
        setAiStatus({ testing: false, ok: true, message: `Connected to AI Provider (${aiModel}).` });
      } else {
        setAiStatus({ testing: false, ok: false, error: res.error || 'Authentication failed. Check your API key.' });
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'AI connection test failed.';
      setAiStatus({ testing: false, ok: false, error: msg });
    }
  };

  const handleTestPlex = async () => {
    setPlexStatus({ testing: true });
    try {
      const res = await testSetupPlex({ url: plexUrl, token: plexToken });
      if (res.ok) {
        const libs = res.music_libraries?.length ? res.music_libraries.join(', ') : 'Music';
        setPlexStatus({ testing: false, ok: true, message: `Connected to Plex (Libraries: ${libs}).` });
      } else {
        setPlexStatus({ testing: false, ok: false, error: res.error || 'Could not connect to Plex.' });
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Plex test failed.';
      setPlexStatus({ testing: false, ok: false, error: msg });
    }
  };

  const handleFinishSetup = async () => {
    setFinishing(true);
    setFinishError(null);
    try {
      // 1. Create admin user
      const adminRes = await submitFirstRunSetup({ username: username.trim(), password });
      if (!adminRes.ok) {
        throw new Error(adminRes.error || 'Failed to create administrator account.');
      }

      // 2. Save configured environment variables
      const envUpdates: Record<string, string> = {};
      if (acoustidKey && !acoustidSkipped) envUpdates['ACOUSTID_API_KEY'] = acoustidKey;
      if (aiApiKey && !aiSkipped) {
        envUpdates['OPENAI_API_KEY'] = aiApiKey;
        if (aiBaseUrl) envUpdates['AI_BASE_URL'] = aiBaseUrl;
        if (aiModel) envUpdates['AI_MODEL'] = aiModel;
      }
      if (plexUrl && plexToken && !plexSkipped) {
        envUpdates['PLEX_URL'] = plexUrl;
        envUpdates['PLEX_TOKEN'] = plexToken;
      }

      if (Object.keys(envUpdates).length > 0) {
        await saveSetupEnv({ variables: envUpdates });
      }

      // 3. Complete setup
      const completeRes = await completeSetup();
      if (!completeRes.ok) {
        throw new Error('Failed to mark setup complete.');
      }

      setFinishSuccess(true);
      setTimeout(() => {
        window.location.href = '/';
      }, 1500);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'An error occurred while finishing setup.';
      setFinishError(msg);
    } finally {
      setFinishing(false);
    }
  };

  const steps = [
    { num: 1, title: 'Welcome' },
    { num: 2, title: 'Admin' },
    { num: 3, title: 'Beets Engine' },
    { num: 4, title: 'Library Paths' },
    { num: 5, title: 'MusicBrainz' },
    { num: 6, title: 'AcoustID' },
    { num: 7, title: 'AI Provider' },
    { num: 8, title: 'Plugins' },
    { num: 9, title: 'Plex' },
    { num: 10, title: 'Review' },
  ];

  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-graphite-950 px-4 py-8 text-zinc-100">
      <div className="w-full max-w-2xl space-y-6">
        {/* Header */}
        <div className="flex flex-col items-center text-center">
          <BeetsLogo />
          <h1 className="mt-3 text-2xl font-black tracking-tight text-white sm:text-3xl">
            Beets Web Manager Setup
          </h1>
          <p className="mt-1 text-sm text-zinc-400">
            Initial application configuration & engine verification
          </p>
        </div>

        {/* Step Indicator */}
        <div className="flex items-center justify-between rounded-xl border border-zinc-800 bg-graphite-900/80 p-2.5 shadow-inner">
          {steps.map((s) => {
            const isCurrent = s.num === currentStep;
            const isDone = s.num < currentStep;
            return (
              <button
                key={s.num}
                onClick={() => {
                  if (isDone) setCurrentStep(s.num);
                }}
                disabled={!isDone}
                title={s.title}
                className={`flex size-7 items-center justify-center rounded-lg text-xs font-bold transition-all ${
                  isCurrent
                    ? 'bg-red-700 text-white shadow-md ring-2 ring-red-500/50'
                    : isDone
                    ? 'bg-zinc-800 text-emerald-400 hover:bg-zinc-700 cursor-pointer'
                    : 'bg-graphite-950 text-zinc-600'
                }`}
              >
                {isDone ? '✓' : s.num}
              </button>
            );
          })}
        </div>

        {/* Wizard Card Body */}
        <div className="rounded-2xl border border-red-900/40 bg-graphite-900/90 p-6 shadow-2xl backdrop-blur sm:p-8">
          {/* STEP 1: Welcome */}
          {currentStep === 1 && (
            <div className="space-y-5">
              <div>
                <h2 className="text-xl font-bold text-white">Welcome to Beets Web Manager</h2>
                <p className="mt-2 text-sm leading-relaxed text-zinc-300">
                  This application manages an existing Beets engine and provides a browser-based interface for importing, matching, reviewing, cleaning, and managing your music library.
                </p>
              </div>

              {/* Architecture Representation */}
              <div className="my-6 rounded-xl border border-zinc-800 bg-graphite-950/80 p-5 text-center">
                <p className="text-xs font-semibold uppercase tracking-wider text-zinc-400 mb-3">
                  System Architecture
                </p>
                <div className="flex items-center justify-center gap-3 text-sm font-mono text-zinc-200">
                  <span className="rounded-lg bg-zinc-800 px-3 py-1.5 border border-zinc-700">Browser</span>
                  <span className="text-red-500 font-bold">&rarr;</span>
                  <span className="rounded-lg bg-red-950/80 px-3 py-1.5 border border-red-800/80 text-red-200 font-semibold">beets-web-manager</span>
                  <span className="text-red-500 font-bold">&rarr;</span>
                  <span className="rounded-lg bg-zinc-800 px-3 py-1.5 border border-zinc-700">Beets Engine</span>
                </div>
              </div>

              <div className="pt-2 flex justify-end">
                <button
                  type="button"
                  onClick={() => setCurrentStep(2)}
                  className="rounded-lg bg-red-700 px-6 py-2.5 text-sm font-bold text-white shadow-md transition-all hover:bg-red-600 focus:outline-none"
                >
                  Get Started &rarr;
                </button>
              </div>
            </div>
          )}

          {/* STEP 2: Administrator Account */}
          {currentStep === 2 && (
            <div className="space-y-5">
              <div>
                <h2 className="text-xl font-bold text-white">Create Administrator Account</h2>
                <p className="mt-1 text-sm text-zinc-400">
                  Set the primary username and password used to sign in to Beets Web Manager.
                </p>
              </div>

              <div className="space-y-4">
                <div>
                  <label className="block text-xs font-semibold uppercase tracking-wider text-zinc-300">
                    Username
                  </label>
                  <input
                    type="text"
                    value={username}
                    onChange={(e) => setUsername(e.target.value)}
                    className="mt-1.5 w-full rounded-lg border border-zinc-700 bg-graphite-950 px-3.5 py-2.5 text-sm text-white focus:border-red-500 focus:outline-none"
                    required
                  />
                </div>

                <div>
                  <label className="block text-xs font-semibold uppercase tracking-wider text-zinc-300">
                    Password
                  </label>
                  <div className="relative mt-1.5">
                    <input
                      type={showPassword ? 'text' : 'password'}
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      className="w-full rounded-lg border border-zinc-700 bg-graphite-950 py-2.5 pl-3.5 pr-10 text-sm text-white focus:border-red-500 focus:outline-none"
                      required
                    />
                    <button
                      type="button"
                      onClick={() => setShowPassword(!showPassword)}
                      className="absolute inset-y-0 right-0 flex items-center pr-3 text-xs font-medium text-zinc-400 hover:text-zinc-200"
                    >
                      {showPassword ? 'Hide' : 'Show'}
                    </button>
                  </div>
                </div>

                <div>
                  <label className="block text-xs font-semibold uppercase tracking-wider text-zinc-300">
                    Confirm Password
                  </label>
                  <input
                    type="password"
                    value={confirmPassword}
                    onChange={(e) => setConfirmPassword(e.target.value)}
                    className="mt-1.5 w-full rounded-lg border border-zinc-700 bg-graphite-950 px-3.5 py-2.5 text-sm text-white focus:border-red-500 focus:outline-none"
                    required
                  />
                  {confirmPassword && !passwordsMatch && (
                    <p className="mt-1 text-xs text-red-400">Passwords do not match</p>
                  )}
                </div>

                {/* Password Requirements Checklist */}
                <div className="rounded-lg border border-zinc-800 bg-graphite-950/70 p-3.5 text-xs">
                  <p className="font-semibold text-zinc-400 mb-2">Password Requirements:</p>
                  <ul className="space-y-1 text-zinc-400">
                    <li className={`flex items-center gap-1.5 ${hasLength ? 'text-emerald-400 font-medium' : ''}`}>
                      <span>{hasLength ? '✓' : '•'}</span> At least {minLength} characters
                    </li>
                    <li className="flex items-center gap-1.5 text-zinc-500">
                      Long passphrases are supported; uppercase, numbers, and symbols are not mandatory.
                    </li>
                  </ul>
                </div>
              </div>

              <div className="pt-2 flex justify-between">
                <button
                  type="button"
                  onClick={() => setCurrentStep(1)}
                  className="rounded-lg bg-zinc-800 px-4 py-2 text-sm font-semibold text-zinc-300 hover:bg-zinc-700"
                >
                  &larr; Back
                </button>
                <button
                  type="button"
                  onClick={() => setCurrentStep(3)}
                  disabled={!adminFormValid}
                  className="rounded-lg bg-red-700 px-6 py-2.5 text-sm font-bold text-white shadow-md transition-all hover:bg-red-600 disabled:opacity-50"
                >
                  Continue &rarr;
                </button>
              </div>
            </div>
          )}

          {/* STEP 3: Beets Engine Connection */}
          {currentStep === 3 && (
            <div className="space-y-5">
              <div>
                <h2 className="text-xl font-bold text-white">Beets Engine Connection</h2>
                <p className="mt-1 text-sm text-zinc-400">
                  Verify that Web Manager can communicate with the background Beets engine container.
                </p>
              </div>

              <div className="rounded-xl border border-zinc-800 bg-graphite-950 p-4 space-y-3">
                <div className="flex items-center justify-between">
                  <div>
                    <span className="text-xs font-semibold uppercase tracking-wider text-zinc-400 block">Service Name</span>
                    <span className="text-sm font-mono text-white">beets</span>
                  </div>
                  <div>
                    <span className="text-xs font-semibold uppercase tracking-wider text-zinc-400 block">Status</span>
                    {beetsStatus.testing ? (
                      <span className="text-xs font-medium text-amber-400">Checking...</span>
                    ) : beetsStatus.ok ? (
                      <span className="inline-flex items-center gap-1 text-xs font-bold text-emerald-400">
                        <span>✓</span> Connected
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1 text-xs font-bold text-red-400">
                        <span>✕</span> Not Connected
                      </span>
                    )}
                  </div>
                </div>

                {beetsStatus.message && (
                  <div className="rounded-lg border border-emerald-500/30 bg-emerald-950/40 p-3 text-xs text-emerald-300">
                    {beetsStatus.message}
                  </div>
                )}

                {beetsStatus.error && (
                  <div className="rounded-lg border border-red-500/30 bg-red-950/40 p-3 text-xs text-red-300 space-y-2">
                    <p>{beetsStatus.error}</p>
                    <button
                      type="button"
                      onClick={() => setShowBeetsDetails(!showBeetsDetails)}
                      className="text-[11px] underline text-red-400 hover:text-red-200"
                    >
                      {showBeetsDetails ? 'Hide technical details' : 'Show technical details'}
                    </button>
                    {showBeetsDetails && (
                      <div className="font-mono text-[11px] text-zinc-400 bg-black/50 p-2 rounded border border-zinc-800">
                        Endpoint: {setupStatus?.beets?.path || 'http://beets:8338'}<br />
                        Reachable: {setupStatus?.beets?.available ? 'Yes' : 'No'}
                      </div>
                    )}
                  </div>
                )}

                <button
                  type="button"
                  onClick={handleTestBeets}
                  disabled={beetsStatus.testing}
                  className="rounded-lg bg-zinc-800 px-4 py-2 text-xs font-bold text-white hover:bg-zinc-700 disabled:opacity-50"
                >
                  {beetsStatus.testing ? 'Testing...' : 'Test Connection'}
                </button>
              </div>

              <div className="pt-2 flex justify-between">
                <button
                  type="button"
                  onClick={() => setCurrentStep(2)}
                  className="rounded-lg bg-zinc-800 px-4 py-2 text-sm font-semibold text-zinc-300 hover:bg-zinc-700"
                >
                  &larr; Back
                </button>
                <button
                  type="button"
                  onClick={() => setCurrentStep(4)}
                  disabled={!beetsStatus.ok}
                  className="rounded-lg bg-red-700 px-6 py-2.5 text-sm font-bold text-white shadow-md hover:bg-red-600 disabled:opacity-50"
                >
                  Continue &rarr;
                </button>
              </div>
            </div>
          )}

          {/* STEP 4: Library Paths */}
          {currentStep === 4 && (
            <div className="space-y-5">
              <div>
                <h2 className="text-xl font-bold text-white">Library Paths</h2>
                <p className="mt-1 text-sm text-zinc-400">
                  Detected path configuration and read/write availability for Beets.
                </p>
              </div>

              <div className="space-y-3">
                {[
                  { name: 'Beets Configuration', key: 'beets_config', path: setupStatus?.paths?.beets_config?.path || '/config/config.yaml', exists: setupStatus?.paths?.beets_config?.exists },
                  { name: 'Music Library', key: 'music_library', path: setupStatus?.paths?.music_library?.path || '/data/media/music', readable: setupStatus?.paths?.music_library?.readable, writable: setupStatus?.paths?.music_library?.writable },
                  { name: 'Import / Staging Directory', key: 'downloads', path: setupStatus?.paths?.downloads?.path || '/data/torrents', readable: setupStatus?.paths?.downloads?.readable, writable: setupStatus?.paths?.downloads?.writable },
                ].map((item) => (
                  <div key={item.key} className="rounded-xl border border-zinc-800 bg-graphite-950 p-3.5 flex items-center justify-between text-xs">
                    <div>
                      <span className="font-semibold text-white block">{item.name}</span>
                      <span className="font-mono text-zinc-400 text-[11px]">{item.path}</span>
                    </div>
                    <div>
                      {item.writable !== undefined ? (
                        item.writable ? (
                          <span className="rounded bg-emerald-950 border border-emerald-800 px-2 py-1 text-emerald-400 font-semibold">Available</span>
                        ) : item.readable ? (
                          <span className="rounded bg-amber-950 border border-amber-800 px-2 py-1 text-amber-400 font-semibold">Read Only</span>
                        ) : (
                          <span className="rounded bg-red-950 border border-red-800 px-2 py-1 text-red-400 font-semibold">Missing / Permission Error</span>
                        )
                      ) : item.exists ? (
                        <span className="rounded bg-emerald-950 border border-emerald-800 px-2 py-1 text-emerald-400 font-semibold">Found</span>
                      ) : (
                        <span className="rounded bg-amber-950 border border-amber-800 px-2 py-1 text-amber-400 font-semibold">Not Found</span>
                      )}
                    </div>
                  </div>
                ))}
              </div>

              <div className="pt-2 flex justify-between">
                <button
                  type="button"
                  onClick={() => setCurrentStep(3)}
                  className="rounded-lg bg-zinc-800 px-4 py-2 text-sm font-semibold text-zinc-300 hover:bg-zinc-700"
                >
                  &larr; Back
                </button>
                <button
                  type="button"
                  onClick={() => setCurrentStep(5)}
                  className="rounded-lg bg-red-700 px-6 py-2.5 text-sm font-bold text-white shadow-md hover:bg-red-600"
                >
                  Continue &rarr;
                </button>
              </div>
            </div>
          )}

          {/* STEP 5: MusicBrainz */}
          {currentStep === 5 && (
            <div className="space-y-5">
              <div>
                <h2 className="text-xl font-bold text-white">MusicBrainz Metadata</h2>
                <p className="mt-1 text-sm text-zinc-400">
                  MusicBrainz is used as the authoritative identity evidence source.
                </p>
              </div>

              <div className="rounded-xl border border-zinc-800 bg-graphite-950 p-4 space-y-3">
                <p className="text-xs text-zinc-300">
                  Public MusicBrainz lookups do not require user credentials.
                </p>

                {mbStatus.message && (
                  <div className="rounded-lg border border-emerald-500/30 bg-emerald-950/40 p-3 text-xs text-emerald-300">
                    {mbStatus.message}
                  </div>
                )}

                {mbStatus.error && (
                  <div className="rounded-lg border border-red-500/30 bg-red-950/40 p-3 text-xs text-red-300">
                    {mbStatus.error}
                  </div>
                )}

                <button
                  type="button"
                  onClick={handleTestMB}
                  disabled={mbStatus.testing}
                  className="rounded-lg bg-zinc-800 px-4 py-2 text-xs font-bold text-white hover:bg-zinc-700 disabled:opacity-50"
                >
                  {mbStatus.testing ? 'Testing...' : 'Test MusicBrainz'}
                </button>
              </div>

              <div className="pt-2 flex justify-between">
                <button
                  type="button"
                  onClick={() => setCurrentStep(4)}
                  className="rounded-lg bg-zinc-800 px-4 py-2 text-sm font-semibold text-zinc-300 hover:bg-zinc-700"
                >
                  &larr; Back
                </button>
                <button
                  type="button"
                  onClick={() => setCurrentStep(6)}
                  className="rounded-lg bg-red-700 px-6 py-2.5 text-sm font-bold text-white shadow-md hover:bg-red-600"
                >
                  Continue &rarr;
                </button>
              </div>
            </div>
          )}

          {/* STEP 6: AcoustID */}
          {currentStep === 6 && (
            <div className="space-y-5">
              <div>
                <h2 className="text-xl font-bold text-white">AcoustID (Optional)</h2>
                <p className="mt-1 text-sm text-zinc-400">
                  AcoustID audio fingerprinting lookup integration.
                </p>
              </div>

              <div className="space-y-4">
                <div>
                  <label className="block text-xs font-semibold uppercase tracking-wider text-zinc-300">
                    AcoustID API Key
                  </label>
                  <input
                    type="text"
                    value={acoustidKey}
                    onChange={(e) => setAcoustidKey(e.target.value)}
                    placeholder="Optional API Key"
                    className="mt-1.5 w-full rounded-lg border border-zinc-700 bg-graphite-950 px-3.5 py-2.5 text-sm text-white focus:border-red-500 focus:outline-none"
                  />
                </div>

                {acoustidStatus.message && (
                  <div className="rounded-lg border border-emerald-500/30 bg-emerald-950/40 p-3 text-xs text-emerald-300">
                    {acoustidStatus.message}
                  </div>
                )}

                {acoustidStatus.error && (
                  <div className="rounded-lg border border-amber-500/30 bg-amber-950/40 p-3 text-xs text-amber-300">
                    {acoustidStatus.error}
                  </div>
                )}

                <button
                  type="button"
                  onClick={handleTestAcoustid}
                  disabled={acoustidStatus.testing}
                  className="rounded-lg bg-zinc-800 px-4 py-2 text-xs font-bold text-white hover:bg-zinc-700 disabled:opacity-50"
                >
                  {acoustidStatus.testing ? 'Testing...' : 'Test AcoustID'}
                </button>
              </div>

              <div className="pt-2 flex justify-between">
                <button
                  type="button"
                  onClick={() => setCurrentStep(5)}
                  className="rounded-lg bg-zinc-800 px-4 py-2 text-sm font-semibold text-zinc-300 hover:bg-zinc-700"
                >
                  &larr; Back
                </button>
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() => {
                      setAcoustidSkipped(true);
                      setCurrentStep(7);
                    }}
                    className="rounded-lg border border-zinc-700 bg-transparent px-4 py-2 text-sm font-semibold text-zinc-300 hover:bg-zinc-800"
                  >
                    Skip for now
                  </button>
                  <button
                    type="button"
                    onClick={() => setCurrentStep(7)}
                    className="rounded-lg bg-red-700 px-6 py-2.5 text-sm font-bold text-white shadow-md hover:bg-red-600"
                  >
                    Continue &rarr;
                  </button>
                </div>
              </div>
            </div>
          )}

          {/* STEP 7: AI Provider Setup */}
          {currentStep === 7 && (
            <div className="space-y-5">
              <div>
                <h2 className="text-xl font-bold text-white">AI Provider (Optional)</h2>
                <p className="mt-1 text-sm text-zinc-400">
                  Configure an optional AI provider for supplemental matching suggestions.
                </p>
              </div>

              <div className="space-y-4">
                <div>
                  <label className="block text-xs font-semibold uppercase tracking-wider text-zinc-300">
                    API Key
                  </label>
                  <input
                    type="password"
                    value={aiApiKey}
                    onChange={(e) => setAiApiKey(e.target.value)}
                    placeholder="sk-..."
                    className="mt-1.5 w-full rounded-lg border border-zinc-700 bg-graphite-950 px-3.5 py-2.5 text-sm text-white focus:border-red-500 focus:outline-none"
                  />
                </div>

                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="block text-xs font-semibold uppercase tracking-wider text-zinc-300">
                      Base URL
                    </label>
                    <input
                      type="text"
                      value={aiBaseUrl}
                      onChange={(e) => setAiBaseUrl(e.target.value)}
                      className="mt-1.5 w-full rounded-lg border border-zinc-700 bg-graphite-950 px-3.5 py-2.5 text-sm text-white focus:border-red-500 focus:outline-none"
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-semibold uppercase tracking-wider text-zinc-300">
                      Model
                    </label>
                    <input
                      type="text"
                      value={aiModel}
                      onChange={(e) => setAiModel(e.target.value)}
                      className="mt-1.5 w-full rounded-lg border border-zinc-700 bg-graphite-950 px-3.5 py-2.5 text-sm text-white focus:border-red-500 focus:outline-none"
                    />
                  </div>
                </div>

                {aiStatus.message && (
                  <div className="rounded-lg border border-emerald-500/30 bg-emerald-950/40 p-3 text-xs text-emerald-300">
                    {aiStatus.message}
                  </div>
                )}

                {aiStatus.error && (
                  <div className="rounded-lg border border-red-500/30 bg-red-950/40 p-3 text-xs text-red-300">
                    {aiStatus.error}
                  </div>
                )}

                <button
                  type="button"
                  onClick={handleTestAi}
                  disabled={aiStatus.testing || !aiApiKey}
                  className="rounded-lg bg-zinc-800 px-4 py-2 text-xs font-bold text-white hover:bg-zinc-700 disabled:opacity-50"
                >
                  {aiStatus.testing ? 'Testing...' : 'Test AI Connection'}
                </button>
              </div>

              <div className="pt-2 flex justify-between">
                <button
                  type="button"
                  onClick={() => setCurrentStep(6)}
                  className="rounded-lg bg-zinc-800 px-4 py-2 text-sm font-semibold text-zinc-300 hover:bg-zinc-700"
                >
                  &larr; Back
                </button>
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() => {
                      setAiSkipped(true);
                      setCurrentStep(8);
                    }}
                    className="rounded-lg border border-zinc-700 bg-transparent px-4 py-2 text-sm font-semibold text-zinc-300 hover:bg-zinc-800"
                  >
                    Skip for now
                  </button>
                  <button
                    type="button"
                    onClick={() => setCurrentStep(8)}
                    className="rounded-lg bg-red-700 px-6 py-2.5 text-sm font-bold text-white shadow-md hover:bg-red-600"
                  >
                    Continue &rarr;
                  </button>
                </div>
              </div>
            </div>
          )}

          {/* STEP 8: Beets Plugins */}
          {currentStep === 8 && (
            <div className="space-y-5">
              <div>
                <h2 className="text-xl font-bold text-white">Beets Plugins</h2>
                <p className="mt-1 text-sm text-zinc-400">
                  Detected plugins in your Beets engine installation.
                </p>
              </div>

              {/* MusicBrainz is core Beets metadata capability, not a
                  togglable plugin -- beets has no "musicbrainz" entry in
                  its plugins: list, so it never reports "configured" the
                  way an optional plugin does. Shown separately as an
                  external service (connected/unavailable) instead of
                  mixed into the plugin grid below, where it would
                  otherwise always render as "not enabled". */}
              {(() => {
                const mbState = setupStatus?.integrations?.musicbrainz?.state || 'not_configured';
                const mbOk = mbState === 'connected' || mbState === 'configured';
                return (
                  <div className="rounded-lg border border-zinc-800 bg-graphite-950 p-3 flex items-center justify-between text-xs">
                    <div>
                      <span className="font-semibold text-white">MusicBrainz</span>
                      <span className="ml-2 text-zinc-500">Core metadata service · no API key required</span>
                    </div>
                    <span className={`px-2 py-0.5 rounded font-bold ${mbOk ? 'bg-emerald-950 text-emerald-400 border border-emerald-800' : 'bg-amber-950 text-amber-400 border border-amber-800'}`}>
                      {mbOk ? '✓ Connected' : '⚠ Beets engine unreachable'}
                    </span>
                  </div>
                );
              })()}

              <div>
                <h3 className="text-xs font-bold uppercase tracking-wide text-zinc-500">Optional Beets Plugins</h3>
              </div>
              <div className="grid grid-cols-2 gap-2.5">
                {[
                  { name: 'chroma / AcoustID', key: 'acoustid', req: false },
                  { name: 'fetchart', key: 'fetchart', req: true },
                  { name: 'embedart', key: 'embedart', req: false },
                  { name: 'scrub', key: 'scrub', req: false },
                  { name: 'zero', key: 'zero', req: false },
                  { name: 'ftintitle', key: 'ftintitle', req: false },
                  { name: 'mbsync', key: 'mbsync', req: false },
                  { name: 'replaygain', key: 'replaygain', req: false },
                ].map((item) => {
                  const state = setupStatus?.integrations?.[item.key]?.state || 'not_configured';
                  const isOk = state === 'configured';
                  return (
                    <div key={item.key} className="rounded-lg border border-zinc-800 bg-graphite-950 p-3 flex items-center justify-between text-xs">
                      <span className="font-semibold text-white">{item.name}</span>
                      <span className={`px-2 py-0.5 rounded font-bold ${isOk ? 'bg-emerald-950 text-emerald-400 border border-emerald-800' : 'bg-zinc-800 text-zinc-400'}`}>
                        {isOk ? '✓ Enabled' : '○ Available'}
                      </span>
                    </div>
                  );
                })}
              </div>

              <div className="pt-2 flex justify-between">
                <button
                  type="button"
                  onClick={() => setCurrentStep(7)}
                  className="rounded-lg bg-zinc-800 px-4 py-2 text-sm font-semibold text-zinc-300 hover:bg-zinc-700"
                >
                  &larr; Back
                </button>
                <button
                  type="button"
                  onClick={() => setCurrentStep(9)}
                  className="rounded-lg bg-red-700 px-6 py-2.5 text-sm font-bold text-white shadow-md hover:bg-red-600"
                >
                  Continue &rarr;
                </button>
              </div>
            </div>
          )}

          {/* STEP 9: Plex Integration */}
          {currentStep === 9 && (
            <div className="space-y-5">
              <div>
                <h2 className="text-xl font-bold text-white">Plex Integration (Optional)</h2>
                <p className="mt-1 text-sm text-zinc-400">
                  Connect Plex Media Server to trigger library updates after library changes.
                </p>
              </div>

              <div className="space-y-4">
                <div>
                  <label className="block text-xs font-semibold uppercase tracking-wider text-zinc-300">
                    Plex URL
                  </label>
                  <input
                    type="text"
                    value={plexUrl}
                    onChange={(e) => setPlexUrl(e.target.value)}
                    placeholder="http://plex.local:32400"
                    className="mt-1.5 w-full rounded-lg border border-zinc-700 bg-graphite-950 px-3.5 py-2.5 text-sm text-white focus:border-red-500 focus:outline-none"
                  />
                </div>

                <div>
                  <label className="block text-xs font-semibold uppercase tracking-wider text-zinc-300">
                    Plex Token
                  </label>
                  <input
                    type="password"
                    value={plexToken}
                    onChange={(e) => setPlexToken(e.target.value)}
                    placeholder="Token"
                    className="mt-1.5 w-full rounded-lg border border-zinc-700 bg-graphite-950 px-3.5 py-2.5 text-sm text-white focus:border-red-500 focus:outline-none"
                  />
                </div>

                {plexStatus.message && (
                  <div className="rounded-lg border border-emerald-500/30 bg-emerald-950/40 p-3 text-xs text-emerald-300">
                    {plexStatus.message}
                  </div>
                )}

                {plexStatus.error && (
                  <div className="rounded-lg border border-red-500/30 bg-red-950/40 p-3 text-xs text-red-300">
                    {plexStatus.error}
                  </div>
                )}

                <button
                  type="button"
                  onClick={handleTestPlex}
                  disabled={plexStatus.testing || !plexUrl || !plexToken}
                  className="rounded-lg bg-zinc-800 px-4 py-2 text-xs font-bold text-white hover:bg-zinc-700 disabled:opacity-50"
                >
                  {plexStatus.testing ? 'Testing...' : 'Test Plex'}
                </button>
              </div>

              <div className="pt-2 flex justify-between">
                <button
                  type="button"
                  onClick={() => setCurrentStep(8)}
                  className="rounded-lg bg-zinc-800 px-4 py-2 text-sm font-semibold text-zinc-300 hover:bg-zinc-700"
                >
                  &larr; Back
                </button>
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() => {
                      setPlexSkipped(true);
                      setCurrentStep(10);
                    }}
                    className="rounded-lg border border-zinc-700 bg-transparent px-4 py-2 text-sm font-semibold text-zinc-300 hover:bg-zinc-800"
                  >
                    Skip for now
                  </button>
                  <button
                    type="button"
                    onClick={() => setCurrentStep(10)}
                    className="rounded-lg bg-red-700 px-6 py-2.5 text-sm font-bold text-white shadow-md hover:bg-red-600"
                  >
                    Review &rarr;
                  </button>
                </div>
              </div>
            </div>
          )}

          {/* STEP 10: Review Configuration & Finish */}
          {currentStep === 10 && (
            <div className="space-y-5">
              <div>
                <h2 className="text-xl font-bold text-white">Review Configuration</h2>
                <p className="mt-1 text-sm text-zinc-400">
                  Review your selections before completing setup.
                </p>
              </div>

              {finishError && (
                <div className="rounded-lg border border-red-500/40 bg-red-950/60 p-3.5 text-xs text-red-200 shadow-sm">
                  <span className="font-semibold">Setup Error: </span>{finishError}
                </div>
              )}

              {finishSuccess ? (
                <div className="rounded-xl border border-emerald-500/30 bg-emerald-950/40 p-6 text-center shadow-lg backdrop-blur">
                  <div className="mx-auto flex size-12 items-center justify-center rounded-full bg-emerald-500/20 text-emerald-400">
                    <svg className="size-6" fill="none" viewBox="0 0 24 24" strokeWidth="2" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" d="m4.5 12.75 6 6 9-13.5" />
                    </svg>
                  </div>
                  <h3 className="mt-3 text-lg font-bold text-emerald-200">Setup Complete!</h3>
                  <p className="mt-1 text-sm text-emerald-400/90">
                    Redirecting to library dashboard...
                  </p>
                </div>
              ) : (
                <>
                  <div className="rounded-xl border border-zinc-800 bg-graphite-950 p-4 divide-y divide-zinc-800 text-xs">
                    <div className="py-2 flex justify-between items-center">
                      <span className="text-zinc-400">Administrator</span>
                      <span className="font-semibold text-white">{username} (Configured)</span>
                    </div>
                    <div className="py-2 flex justify-between items-center">
                      <span className="text-zinc-400">Beets Engine</span>
                      <span className={`font-semibold ${beetsStatus.ok ? 'text-emerald-400' : 'text-amber-400'}`}>
                        {beetsStatus.ok ? beetsStatus.message : 'Checking/Unverified'}
                      </span>
                    </div>
                    <div className="py-2 flex justify-between items-center">
                      <span className="text-zinc-400">Music Library</span>
                      <span className="font-mono text-zinc-300">{setupStatus?.paths?.music_library?.path || '/data/media/music'}</span>
                    </div>
                    <div className="py-2 flex justify-between items-center">
                      <span className="text-zinc-400">MusicBrainz</span>
                      <span className="font-semibold text-emerald-400">Connected</span>
                    </div>
                    <div className="py-2 flex justify-between items-center">
                      <span className="text-zinc-400">AcoustID</span>
                      <span className="font-semibold text-zinc-300">
                        {acoustidSkipped ? 'Not configured (Skipped)' : (acoustidKey ? 'Configured' : 'Default test key')}
                      </span>
                    </div>
                    <div className="py-2 flex justify-between items-center">
                      <span className="text-zinc-400">AI Provider</span>
                      <span className="font-semibold text-zinc-300">
                        {aiSkipped ? 'Not configured (Skipped)' : (aiApiKey ? `Configured (${aiModel})` : 'Not configured')}
                      </span>
                    </div>
                    <div className="py-2 flex justify-between items-center">
                      <span className="text-zinc-400">Plex</span>
                      <span className="font-semibold text-zinc-300">
                        {plexSkipped ? 'Not configured (Skipped)' : (plexUrl && plexToken ? 'Configured' : 'Not configured')}
                      </span>
                    </div>
                  </div>

                  <div className="pt-2 flex justify-between">
                    <button
                      type="button"
                      onClick={() => setCurrentStep(9)}
                      className="rounded-lg bg-zinc-800 px-4 py-2 text-sm font-semibold text-zinc-300 hover:bg-zinc-700"
                    >
                      &larr; Back
                    </button>
                    <button
                      type="button"
                      onClick={handleFinishSetup}
                      disabled={finishing}
                      className="rounded-lg bg-red-700 px-6 py-2.5 text-sm font-bold text-white shadow-md hover:bg-red-600 disabled:opacity-50"
                    >
                      {finishing ? 'Finishing Setup...' : 'Finish Setup'}
                    </button>
                  </div>
                </>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
