import { errorMessage } from '../lib/errors';
import { useState } from 'react';
import { login } from '../api/client';
import { RecordMark } from '../components/RecordMark';

interface LoginProps {
  onSuccess?: () => void;
}

export default function Login({ onSuccess }: LoginProps) {
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [remember, setRemember] = useState(true);
  const [showPassword, setShowPassword] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErrorMsg(null);

    const cleanUser = username.trim();
    if (!cleanUser || !password) {
      setErrorMsg('Please enter both username and password.');
      return;
    }

    setSubmitting(true);
    try {
      const res = await login({ username: cleanUser, password, remember });
      if (res.ok) {
        if (onSuccess) {
          onSuccess();
        } else {
          window.location.href = '/';
        }
      } else {
        setErrorMsg(res.error || 'Incorrect username or password.');
      }
    } catch (err: unknown) {
      const msg = errorMessage(err, 'Sign-in failed. Please try again.');
      setErrorMsg(msg.includes('401') ? 'Incorrect username or password.' : msg);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="flex min-h-screen flex-col items-center justify-center bg-graphite-950 px-4 py-12 text-zinc-100">
      <div className="w-full max-w-sm">
        <div className="flex items-center gap-3.5">
          <RecordMark className="size-12" />
          <div className="min-w-0">
            <h1 className="font-display text-2xl leading-tight font-semibold text-brass-200">
              Beets Web Manager
            </h1>
            <p className="mt-0.5 text-sm text-zinc-400">
              Manage your Beets music library
            </p>
          </div>
        </div>

        <form
          onSubmit={handleSubmit}
          className="mt-7 rounded-lg border border-graphite-700 bg-graphite-900 p-5 sm:p-6"
        >
          {errorMsg && (
            <div role="alert" className="mb-5 rounded-md border border-red-500/40 bg-red-950/60 p-3 text-sm text-red-200">
              <span className="font-semibold">Sign-In Error: </span>
              {errorMsg}
            </div>
          )}

          <div className="space-y-4">
            <div>
              <label
                htmlFor="login-username"
                className="block text-sm font-semibold text-zinc-200"
              >
                Username
              </label>
              <input
                id="login-username"
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                className="mt-1.5 h-11 w-full rounded-md border border-field-border bg-graphite-950 px-3 text-base text-zinc-50 placeholder:text-muted focus:border-brass-400 sm:text-sm"
                required
                autoFocus
              />
            </div>

            <div>
              <label
                htmlFor="login-password"
                className="block text-sm font-semibold text-zinc-200"
              >
                Password
              </label>
              <div className="relative mt-1.5">
                <input
                  id="login-password"
                  type={showPassword ? 'text' : 'password'}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="h-11 w-full rounded-md border border-field-border bg-graphite-950 pr-16 pl-3 text-base text-zinc-50 placeholder:text-muted focus:border-brass-400 sm:text-sm"
                  required
                />
                <button
                  type="button"
                  aria-controls="login-password"
                  onClick={() => setShowPassword(!showPassword)}
                  className="absolute inset-y-0 right-0 flex min-w-14 items-center justify-center rounded-r-md px-3 text-sm font-medium text-zinc-300 hover:text-zinc-50"
                >
                  {showPassword ? 'Hide' : 'Show'}
                </button>
              </div>
            </div>

            <label className="flex min-h-11 cursor-pointer items-center gap-2.5 text-sm text-zinc-300 select-none">
              <input
                type="checkbox"
                checked={remember}
                onChange={(e) => setRemember(e.target.checked)}
                className="size-4 accent-brass-500"
              />
              Remember me
            </label>

            <button
              type="submit"
              disabled={submitting || !username.trim() || !password}
              className="h-11 w-full rounded-md bg-brass-500 px-4 text-sm font-semibold text-graphite-950 transition-colors hover:bg-brass-400 active:bg-brass-600 disabled:cursor-not-allowed disabled:bg-graphite-900 disabled:text-graphite-400 disabled:ring-1 disabled:ring-graphite-700"
            >
              {submitting ? 'Signing In...' : 'Sign In'}
            </button>
          </div>
        </form>

        <p className="mt-6 text-center text-xs text-muted">
          Beets Web Manager &bull; Administrator Access Only
        </p>
      </div>
    </main>
  );
}
