import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as client from '../src/api/client';
import { AccountSection } from '../src/features/system/AccountSection';

// Settings > Account: current password is always required; the new password is
// confirmed before submit; the client is called with only the changed fields.

vi.mock('../src/api/client', () => ({
  changeAccountCredentials: vi.fn(),
}));

const mocked = vi.mocked(client);

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const submitButton = () => screen.getByRole('button', { name: /Update credentials|Saving/ });

describe('AccountSection', () => {
  beforeEach(() => {
    mocked.changeAccountCredentials.mockResolvedValue({ ok: true, message: 'Updated password.', password_changed: true });
  });

  it('keeps submit disabled until current password and a change are present', () => {
    render(<AccountSection />);
    expect((submitButton() as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(screen.getByLabelText('Current password'), { target: { value: 'old-passphrase-123456' } });
    // Current password alone is not a change.
    expect((submitButton() as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(screen.getByLabelText('New username'), { target: { value: 'curator' } });
    expect((submitButton() as HTMLButtonElement).disabled).toBe(false);
  });

  it('blocks submit while the new password and confirmation differ', () => {
    render(<AccountSection />);
    fireEvent.change(screen.getByLabelText('Current password'), { target: { value: 'old-passphrase-123456' } });
    fireEvent.change(screen.getByLabelText('New password'), { target: { value: 'new-passphrase-abcdefgh' } });
    fireEvent.change(screen.getByLabelText('Confirm new password'), { target: { value: 'different' } });
    expect((submitButton() as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText('Passwords do not match.')).toBeTruthy();

    fireEvent.change(screen.getByLabelText('Confirm new password'), { target: { value: 'new-passphrase-abcdefgh' } });
    expect((submitButton() as HTMLButtonElement).disabled).toBe(false);
  });

  it('submits only the changed fields and shows success', async () => {
    render(<AccountSection />);
    fireEvent.change(screen.getByLabelText('Current password'), { target: { value: 'old-passphrase-123456' } });
    fireEvent.change(screen.getByLabelText('New password'), { target: { value: 'new-passphrase-abcdefgh' } });
    fireEvent.change(screen.getByLabelText('Confirm new password'), { target: { value: 'new-passphrase-abcdefgh' } });
    fireEvent.click(submitButton());

    await waitFor(() => expect(mocked.changeAccountCredentials).toHaveBeenCalledTimes(1));
    expect(mocked.changeAccountCredentials).toHaveBeenCalledWith({
      current_password: 'old-passphrase-123456',
      password: 'new-passphrase-abcdefgh',
    });
    expect(await screen.findByText('Updated password.')).toBeTruthy();
    // Fields clear after success.
    expect((screen.getByLabelText('Current password') as HTMLInputElement).value).toBe('');
  });

  it('surfaces the server error and unmet requirements', async () => {
    mocked.changeAccountCredentials.mockResolvedValue({
      ok: false,
      error: 'Password does not meet requirements: at least 16 characters',
      unmet_requirements: ['at least 16 characters'],
    });
    render(<AccountSection />);
    fireEvent.change(screen.getByLabelText('Current password'), { target: { value: 'old-passphrase-123456' } });
    fireEvent.change(screen.getByLabelText('New password'), { target: { value: 'short' } });
    fireEvent.change(screen.getByLabelText('Confirm new password'), { target: { value: 'short' } });
    fireEvent.click(submitButton());

    expect(await screen.findByText(/Password does not meet requirements/)).toBeTruthy();
    expect(screen.getByText('at least 16 characters')).toBeTruthy();
  });
});
