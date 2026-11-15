import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import Login from '../src/views/Login';
import FirstRunSetup from '../src/components/FirstRunSetup';

vi.mock('../src/api/client', async (importActual) => ({
  ...(await importActual<typeof import('../src/api/client')>()),
  login: vi.fn(),
  // FirstRunSetup fetches this on mount; resolve empty so the wizard renders.
  getSetupStatus: vi.fn(() => Promise.resolve({})),
  getPluginsStatus: vi.fn(() => new Promise(() => {})),
}));

afterEach(cleanup);

// Password-manager autofill + a11y: credential inputs must carry the correct
// autocomplete tokens. Sign-in reuses an existing credential; first-run creates one.
describe('credential autocomplete', () => {
  it('sign-in form offers the existing username/current-password', () => {
    render(<Login onSuccess={() => {}} />);
    expect(screen.getByLabelText('Username').getAttribute('autocomplete')).toBe('username');
    expect(screen.getByLabelText('Password').getAttribute('autocomplete')).toBe('current-password');
  });

  it('first-run admin creation offers username and new-password on both password fields', () => {
    render(<FirstRunSetup />);
    fireEvent.click(screen.getByRole('button', { name: /Get Started/ }));
    expect(screen.getByLabelText('Username').getAttribute('autocomplete')).toBe('username');
    expect(screen.getByLabelText('Password').getAttribute('autocomplete')).toBe('new-password');
    expect(screen.getByLabelText('Confirm password').getAttribute('autocomplete')).toBe('new-password');
  });
});
