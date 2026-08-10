import { useState } from 'react';
import { submitFirstRunSetup } from '../api/client';

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
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  const hasLength = password.length >= 32;
  const hasUpper = /[A-Z]/.test(password);
  const hasLower = /[a-z]/.test(password);
  const hasDigit = /[0-9]/.test(password);
  const hasSpecial = /[^A-Za-z0-9]/.test(password);
  const matches = password !== '' && password === confirmPassword;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErrorMsg(null);

    const cleanUser = username.trim();
    if (!cleanUser) {
      setErrorMsg('Please provide a username.');
      return;
    }
    if (!matches) {
      setErrorMsg('Passwords do not match.');
      return;
    }
    if (!hasLength || !hasUpper || !hasLower || !hasDigit || !hasSpecial) {
      setErrorMsg('Password does not meet complexity requirements.');
      return;
    }

    setSubmitting(true);
    try {
      const res = await submitFirstRunSetup({
        username: cleanUser,
        password,
      });

      if (res.ok) {
        setSuccess(true);
        setTimeout(() => {
          window.location.href = '/';
        }, 1500);
      } else {
        setErrorMsg(res.error || 'Failed to complete setup.');
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'An error occurred during setup.';
      setErrorMsg(msg);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-graphite-950 px-4 py-12 text-zinc-100">
      <div className="w-full max-w-md space-y-6">
        <div className="flex flex-col items-center text-center">
          <BeetsLogo />
          <h1 className="mt-4 text-2xl font-black tracking-tight text-white sm:text-3xl">
            Finish Beets Web Manager Setup
          </h1>
          <p className="mt-2 text-sm text-zinc-400">
            Create the administrator login you&apos;ll use to access Beets Web Manager.
          </p>
        </div>

        {success ? (
          <div className="rounded-xl border border-emerald-500/30 bg-emerald-950/40 p-6 text-center shadow-lg backdrop-blur">
            <div className="mx-auto flex size-12 items-center justify-center rounded-full bg-emerald-500/20 text-emerald-400">
              <svg className="size-6" fill="none" viewBox="0 0 24 24" strokeWidth="2" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" d="m4.5 12.75 6 6 9-13.5" />
              </svg>
            </div>
            <h2 className="mt-3 text-lg font-bold text-emerald-200">Setup Complete!</h2>
            <p className="mt-1 text-sm text-emerald-400/90">
              Redirecting to sign in...
            </p>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="rounded-2xl border border-red-900/40 bg-graphite-900/90 p-6 shadow-2xl backdrop-blur">
            {errorMsg && (
              <div className="mb-5 rounded-lg border border-red-500/40 bg-red-950/60 p-3.5 text-xs text-red-200 shadow-sm">
                <span className="font-semibold">Setup Error: </span>{errorMsg}
              </div>
            )}

            <div className="space-y-4">
              <div>
                <label className="block text-xs font-semibold uppercase tracking-wider text-zinc-300">
                  Username
                </label>
                <input
                  type="text"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  className="mt-1.5 w-full rounded-lg border border-zinc-700 bg-graphite-950 px-3.5 py-2.5 text-sm text-white placeholder-zinc-500 focus:border-red-500 focus:outline-none focus:ring-1 focus:ring-red-500"
                  required
                  autoFocus
                />
              </div>

              <div>
                <label className="block text-xs font-semibold uppercase tracking-wider text-zinc-300">
                  Password
                </label>
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="mt-1.5 w-full rounded-lg border border-zinc-700 bg-graphite-950 px-3.5 py-2.5 text-sm text-white placeholder-zinc-500 focus:border-red-500 focus:outline-none focus:ring-1 focus:ring-red-500"
                  required
                />
              </div>

              <div>
                <label className="block text-xs font-semibold uppercase tracking-wider text-zinc-300">
                  Confirm Password
                </label>
                <input
                  type="password"
                  value={confirmPassword}
                  onChange={(e) => setConfirmPassword(e.target.value)}
                  className="mt-1.5 w-full rounded-lg border border-zinc-700 bg-graphite-950 px-3.5 py-2.5 text-sm text-white placeholder-zinc-500 focus:border-red-500 focus:outline-none focus:ring-1 focus:ring-red-500"
                  required
                />
                {confirmPassword && !matches && (
                  <p className="mt-1 text-xs text-red-400">Passwords do not match</p>
                )}
              </div>

              <div className="rounded-lg border border-zinc-800 bg-graphite-950/70 p-3.5 text-xs">
                <p className="font-semibold text-zinc-400 mb-2">Password Requirements:</p>
                <ul className="space-y-1 text-zinc-400">
                  <li className={`flex items-center gap-1.5 ${hasLength ? 'text-emerald-400 font-medium' : ''}`}>
                    <span>{hasLength ? '✓' : '•'}</span> At least 32 characters
                  </li>
                  <li className={`flex items-center gap-1.5 ${hasUpper ? 'text-emerald-400 font-medium' : ''}`}>
                    <span>{hasUpper ? '✓' : '•'}</span> An uppercase letter (A-Z)
                  </li>
                  <li className={`flex items-center gap-1.5 ${hasLower ? 'text-emerald-400 font-medium' : ''}`}>
                    <span>{hasLower ? '✓' : '•'}</span> A lowercase letter (a-z)
                  </li>
                  <li className={`flex items-center gap-1.5 ${hasDigit ? 'text-emerald-400 font-medium' : ''}`}>
                    <span>{hasDigit ? '✓' : '•'}</span> A number (0-9)
                  </li>
                  <li className={`flex items-center gap-1.5 ${hasSpecial ? 'text-emerald-400 font-medium' : ''}`}>
                    <span>{hasSpecial ? '✓' : '•'}</span> A special character (!@#$%...)
                  </li>
                </ul>
              </div>

              <button
                type="submit"
                disabled={submitting || !matches || !hasLength || !hasUpper || !hasLower || !hasDigit || !hasSpecial}
                className="mt-2 w-full rounded-lg bg-red-700 px-4 py-3 text-sm font-bold text-white shadow-md transition-all hover:bg-red-600 focus:outline-none focus:ring-2 focus:ring-red-500 focus:ring-offset-2 focus:ring-offset-graphite-900 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {submitting ? 'Creating Login...' : 'Create Login & Complete Setup'}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
