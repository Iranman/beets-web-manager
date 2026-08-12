import { useState } from 'react';
import { login } from '../api/client';

function BeetsLogo() {
  return (
    <span className="flex size-14 items-center justify-center rounded-2xl bg-red-700 text-white shadow-xl shadow-red-950/60 ring-1 ring-red-400/40">
      <svg className="size-8" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M12 7.4c3.5 0 6.1 2.9 5.5 6.3-.5 3.4-2.8 6.2-5.5 6.2s-5-2.8-5.5-6.2c-.6-3.4 2-6.3 5.5-6.3Z" fill="currentColor" />
        <path d="M12 7.9c-.2-2.5.8-4.1 3-4.8.3 2.3-.8 3.9-3 4.8Z" fill="#fca5a5" />
        <path d="M11.8 7.9c-2.4-.2-3.9-1.3-4.6-3.2 2.3-.1 3.9.9 4.6 3.2Z" fill="#fecaca" />
        <path d="M12 7.7V4.4" stroke="#fee2e2" strokeWidth="1.4" strokeLinecap="round" />
      </svg>
    </span>
  );
}

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
      const msg = err instanceof Error ? err.message : 'Sign-in failed. Please try again.';
      setErrorMsg(msg.includes('401') ? 'Incorrect username or password.' : msg);
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
            Beets Web Manager
          </h1>
          <p className="mt-1.5 text-sm text-zinc-400">
            Manage your Beets music library
          </p>
        </div>

        <form
          onSubmit={handleSubmit}
          className="rounded-2xl border border-red-900/40 bg-graphite-900/90 p-6 shadow-2xl backdrop-blur sm:p-8"
        >
          {errorMsg && (
            <div className="mb-5 rounded-lg border border-red-500/40 bg-red-950/60 p-3.5 text-xs text-red-200 shadow-sm">
              <span className="font-semibold">Sign-In Error: </span>
              {errorMsg}
            </div>
          )}

          <div className="space-y-4">
            <div>
              <label
                htmlFor="login-username"
                className="block text-xs font-semibold uppercase tracking-wider text-zinc-300"
              >
                Username
              </label>
              <input
                id="login-username"
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                className="mt-1.5 w-full rounded-lg border border-zinc-700 bg-graphite-950 px-3.5 py-2.5 text-sm text-white placeholder-zinc-500 focus:border-red-500 focus:outline-none focus:ring-1 focus:ring-red-500"
                required
                autoFocus
              />
            </div>

            <div>
              <label
                htmlFor="login-password"
                className="block text-xs font-semibold uppercase tracking-wider text-zinc-300"
              >
                Password
              </label>
              <div className="relative mt-1.5">
                <input
                  id="login-password"
                  type={showPassword ? 'text' : 'password'}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="w-full rounded-lg border border-zinc-700 bg-graphite-950 py-2.5 pl-3.5 pr-10 text-sm text-white placeholder-zinc-500 focus:border-red-500 focus:outline-none focus:ring-1 focus:ring-red-500"
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

            <div className="flex items-center justify-between pt-1">
              <label className="flex items-center gap-2 text-xs text-zinc-300 cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={remember}
                  onChange={(e) => setRemember(e.target.checked)}
                  className="size-4 rounded border-zinc-700 bg-graphite-950 text-red-600 focus:ring-red-500 focus:ring-offset-graphite-900"
                />
                Remember me
              </label>
            </div>

            <button
              type="submit"
              disabled={submitting || !username.trim() || !password}
              className="mt-2 w-full rounded-lg bg-red-700 px-4 py-3 text-sm font-bold text-white shadow-md transition-all hover:bg-red-600 focus:outline-none focus:ring-2 focus:ring-red-500 focus:ring-offset-2 focus:ring-offset-graphite-900 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {submitting ? 'Signing In...' : 'Sign In'}
            </button>
          </div>
        </form>

        <p className="text-center text-xs text-zinc-500">
          Beets Web Manager &bull; Administrator Access Only
        </p>
      </div>
    </div>
  );
}
