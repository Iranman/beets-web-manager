import { errorMessage } from '../../lib/errors';
import { Dialog, DialogBackdrop, DialogPanel, DialogTitle } from '@headlessui/react';
import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import { useCallback, useEffect, useState } from 'react';
import { getSetupStatus, regenerateAuthToken } from '../../api/client';
import type { SetupStatusResponse } from '../../api/types';
import { EnvEditor, SECURITY_SECTION } from './EnvEditor';

// Settings > Security: sign-in and API token status, token regeneration, and the
// "Authentication & Security" environment variables (password, username, token, outbound policy).
// Moved from the System page; the confirmation dialog and the shown-once token dialog are unchanged.

const onlySecurity = (section: string) => section === SECURITY_SECTION;

function AuthTokenDialog({
  open,
  token,
  warning,
  onClose,
}: {
  open: boolean;
  token: string;
  warning: string;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);
  return (
    <Dialog open={open} onClose={() => undefined} className="relative z-50">
      <DialogBackdrop className="fixed inset-0 bg-graphite-950/60" />
      <div className="fixed inset-0 flex items-center justify-center p-4">
        <DialogPanel className="w-full max-w-lg rounded-lg border border-graphite-700 bg-graphite-900 p-5 shadow-2xl">
          <DialogTitle className="text-base font-semibold text-zinc-100">New API token generated</DialogTitle>
          <p className="mt-2 text-sm font-semibold text-amber-300">{warning || 'Save this token now — it will not be shown again.'}</p>
          <div className="mt-3 break-all rounded border border-graphite-700 bg-graphite-950 p-3 font-mono text-sm text-emerald-300">
            {token}
          </div>
          <p className="mt-2 text-xs text-muted">
            Use this as a Bearer token (Authorization: Bearer &lt;token&gt;) for API/script clients. It replaces the
            previous token immediately.
          </p>
          <div className="mt-5 flex justify-end gap-2">
            <Button
              variant="outlined"
              size="small"
              onClick={() => {
                void navigator.clipboard?.writeText(token).then(() => setCopied(true)).catch(() => undefined);
              }}
            >
              {copied ? 'Copied' : 'Copy'}
            </Button>
            <Button variant="contained" size="small" onClick={onClose}>
              I've saved it — close
            </Button>
          </div>
        </DialogPanel>
      </div>
    </Dialog>
  );
}

export function SecuritySection() {
  const [status, setStatus] = useState<SetupStatusResponse | null>(null);
  const [error, setError] = useState('');
  const [regeneratingToken, setRegeneratingToken] = useState(false);
  const [regenerateTokenError, setRegenerateTokenError] = useState('');
  const [confirmRegenerate, setConfirmRegenerate] = useState(false);
  const [revealedToken, setRevealedToken] = useState<{ token: string; warning: string } | null>(null);

  const load = useCallback(async () => {
    setError('');
    try {
      setStatus(await getSetupStatus());
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const handleRegenerateToken = async () => {
    setConfirmRegenerate(false);
    setRegeneratingToken(true);
    setRegenerateTokenError('');
    try {
      const result = await regenerateAuthToken();
      if (result.ok && result.token) {
        setRevealedToken({ token: result.token, warning: result.warning || '' });
        await load();
      } else {
        setRegenerateTokenError(result.error || 'Could not regenerate token.');
      }
    } catch (err) {
      setRegenerateTokenError(errorMessage(err));
    } finally {
      setRegeneratingToken(false);
    }
  };

  return (
    <div className="space-y-4">
      {error && <Alert severity="error">{`Sign-in status could not be loaded: ${error}`}</Alert>}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="m-0 max-w-[65ch] text-sm text-muted">
          Who can sign in, how scripts reach the API, and the outbound request policy.
        </p>
        <Button
          variant="outlined"
          size="small"
          color="warning"
          onClick={() => setConfirmRegenerate(true)}
          disabled={regeneratingToken}
        >
          {regeneratingToken ? 'Regenerating...' : 'Regenerate API token'}
        </Button>
      </div>
      {regenerateTokenError && <Alert severity="error" onClose={() => setRegenerateTokenError('')}>{regenerateTokenError}</Alert>}
      {status && (
        <dl className="m-0 divide-y divide-graphite-800 border-y border-graphite-800">
          <div className="grid gap-1 py-2 sm:grid-cols-[14rem_minmax(0,1fr)] sm:gap-4">
            <dt className="text-sm font-medium text-zinc-300">API token (Bearer)</dt>
            <dd className="m-0 min-w-0 text-xs">
              <div className={`font-semibold ${status.auth.token_configured ? 'text-emerald-300' : 'text-muted'}`}>
                {status.auth.token_configured
                  ? status.auth.token_auto_generated ? '✓ Configured (auto-generated)' : '✓ Configured'
                  : '✗ Not configured'}
              </div>
              {status.auth.token_auto_generated && (
                <div className="mt-1 text-muted">
                  A secure token was generated automatically on first run and printed once to the server log.
                  Regenerate it here any time.
                </div>
              )}
            </dd>
          </div>
          <div className="grid gap-1 py-2 sm:grid-cols-[14rem_minmax(0,1fr)] sm:gap-4">
            <dt className="text-sm font-medium text-zinc-300">Browser Login (Basic Auth)</dt>
            <dd className="m-0 min-w-0 text-xs">
              <div className={`font-semibold ${status.auth.password_configured ? 'text-emerald-300' : 'text-muted'}`}>
                {status.auth.password_configured
                  ? status.auth.password_auto_generated ? '✓ Configured (auto-generated)' : '✓ Configured'
                  : '✗ Not configured'}
              </div>
              <div className="mt-1 text-muted">
                {status.auth.password_auto_generated
                  ? `Username: ${status.auth.username || 'admin'}. Initial password was generated on first boot in /web-manager-data/.initial_admin_password. Set BEETS_WEB_PASSWORD below to replace it.`
                  : `Username: ${status.auth.username || 'admin'}. Set BEETS_WEB_PASSWORD or BEETS_WEB_USERNAME below to change browser credentials.`}
              </div>
            </dd>
          </div>
        </dl>
      )}

      <Dialog open={confirmRegenerate} onClose={() => setConfirmRegenerate(false)} className="relative z-50">
        <DialogBackdrop className="fixed inset-0 bg-graphite-950/60" />
        <div className="fixed inset-0 flex items-center justify-center p-4">
          <DialogPanel className="w-full max-w-sm rounded-lg border border-graphite-700 bg-graphite-900 p-5 shadow-2xl">
            <DialogTitle className="text-base font-semibold text-zinc-100">Regenerate API token?</DialogTitle>
            <p className="mt-2 text-sm text-zinc-400">
              The current token stops working immediately. Any script or API client using it will need the new value.
            </p>
            <div className="mt-5 flex justify-end gap-2">
              <Button variant="outlined" size="small" onClick={() => setConfirmRegenerate(false)}>Cancel</Button>
              <Button variant="contained" size="small" color="warning" onClick={() => void handleRegenerateToken()}>
                Regenerate
              </Button>
            </div>
          </DialogPanel>
        </div>
      </Dialog>

      <AuthTokenDialog
        open={revealedToken !== null}
        token={revealedToken?.token ?? ''}
        warning={revealedToken?.warning ?? ''}
        onClose={() => setRevealedToken(null)}
      />

      <EnvEditor include={onlySecurity} saveLabel="Save security settings" onSaved={() => void load()} />
    </div>
  );
}
