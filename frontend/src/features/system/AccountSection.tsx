import { errorMessage } from '../../lib/errors';
import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import TextField from '@mui/material/TextField';
import { useState } from 'react';
import { changeAccountCredentials } from '../../api/client';

// Settings > Account: change the administrator sign-in username and/or password
// from inside the app. The current password is required and re-verified by the
// server (rate-limited) before any change; the new password is stored only as a
// hash. Nothing here is sent or shown in plaintext beyond the form itself.

export function AccountSection() {
  const [current, setCurrent] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [unmet, setUnmet] = useState<string[]>([]);

  const wantsPassword = password.trim() !== '';
  const wantsUsername = username.trim() !== '';
  const passwordsMatch = password === confirm;
  const canSubmit =
    current.trim() !== '' &&
    (wantsPassword || wantsUsername) &&
    (!wantsPassword || passwordsMatch) &&
    !busy;

  const reset = () => {
    setCurrent('');
    setUsername('');
    setPassword('');
    setConfirm('');
  };

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!canSubmit) return;
    setBusy(true);
    setMessage('');
    setError('');
    setUnmet([]);
    try {
      const result = await changeAccountCredentials({
        current_password: current,
        ...(wantsUsername ? { username: username.trim() } : {}),
        ...(wantsPassword ? { password } : {}),
      });
      if (result.ok) {
        const parts = [
          result.username_changed ? 'username' : '',
          result.password_changed ? 'password' : '',
        ].filter(Boolean);
        setMessage(result.message || `Updated ${parts.join(' and ')}.`);
        reset();
      } else {
        setError(result.error || 'Could not update credentials.');
        setUnmet(result.unmet_requirements ?? []);
      }
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="max-w-xl space-y-4" onSubmit={handleSubmit}>
      <p className="m-0 max-w-[65ch] text-sm text-muted">
        Change the browser sign-in username or password without editing environment variables or
        recreating the container. Your current password is required to confirm the change.
      </p>

      {message && <Alert severity="success" onClose={() => setMessage('')}>{message}</Alert>}
      {error && (
        <Alert severity="error" onClose={() => setError('')}>
          {error}
          {unmet.length > 0 && (
            <ul className="m-0 mt-1 list-disc pl-5">
              {unmet.map((item) => <li key={item}>{item}</li>)}
            </ul>
          )}
        </Alert>
      )}

      {/* Hidden username hint for password managers even when the field is left blank. */}
      <input type="text" name="username" autoComplete="username" value={username} readOnly hidden aria-hidden="true" />

      <label className="block space-y-1">
        <span className="liner-label">Current password</span>
        <TextField
          fullWidth
          size="small"
          type="password"
          value={current}
          onChange={(event) => setCurrent(event.target.value)}
          autoComplete="current-password"
          required
          slotProps={{ htmlInput: { 'aria-label': 'Current password' } }}
        />
      </label>

      <label className="block space-y-1">
        <span className="liner-label">New username</span>
        <TextField
          fullWidth
          size="small"
          type="text"
          value={username}
          onChange={(event) => setUsername(event.target.value)}
          autoComplete="username"
          placeholder="Leave blank to keep the current username"
          slotProps={{ htmlInput: { 'aria-label': 'New username', maxLength: 64 } }}
        />
      </label>

      <label className="block space-y-1">
        <span className="liner-label">New password</span>
        <TextField
          fullWidth
          size="small"
          type="password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          autoComplete="new-password"
          placeholder="Leave blank to keep the current password"
          slotProps={{ htmlInput: { 'aria-label': 'New password' } }}
        />
      </label>

      <label className="block space-y-1">
        <span className="liner-label">Confirm new password</span>
        <TextField
          fullWidth
          size="small"
          type="password"
          value={confirm}
          onChange={(event) => setConfirm(event.target.value)}
          autoComplete="new-password"
          error={wantsPassword && !passwordsMatch}
          helperText={wantsPassword && !passwordsMatch ? 'Passwords do not match.' : ' '}
          slotProps={{ htmlInput: { 'aria-label': 'Confirm new password' } }}
        />
      </label>

      <div className="flex justify-end">
        <Button type="submit" variant="contained" size="small" disabled={!canSubmit}>
          {busy ? 'Saving...' : 'Update credentials'}
        </Button>
      </div>
    </form>
  );
}
