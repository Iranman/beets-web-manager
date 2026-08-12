import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, useSearchParams } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AppRoutes } from '../src/App';

// @testing-library/react's auto-cleanup registers with the test runner's
// *global* afterEach, which this project intentionally does not enable
// (vitest.config.ts sets globals: false to avoid polluting the rest of the
// TypeScript project with ambient test globals). Without this explicit
// call, DOM from one test leaks into the next.
afterEach(cleanup);

// Shell pulls in real API/hook calls (job polling, health checks) that must
// not hit the network in a unit test. Only these two dependencies are
// mocked; Shell itself (including its real NavLink elements) renders as-is
// so navigation-destination behavior is genuinely exercised.
vi.mock('../src/lib/hooks', () => ({
  useGlobalJobs: () => ({ running: 0, failed: 0, refresh: () => {} }),
}));
vi.mock('../src/api/client', () => ({
  getHealth: vi.fn().mockResolvedValue({ ok: true }),
  restartApp: vi.fn().mockResolvedValue({ ok: true }),
}));

// Each top-level page view is mocked to a lightweight marker component.
// This test suite verifies react-router v8's route matching/redirect/
// query-param/navigation behavior -- not each view's own internal data
// fetching, which is exercised elsewhere (existing Python static tests,
// manual browser smoke testing).
vi.mock('../src/views/Library', () => ({ default: () => <div>LIBRARY_PAGE</div> }));
vi.mock('../src/views/LibraryChanges', () => ({ default: () => <div>CHANGES_PAGE</div> }));
function MockImportPage() {
  const [params] = useSearchParams();
  return (
    <div>
      IMPORT_PAGE tab={params.get('tab') ?? ''} filter={params.get('filter') ?? ''} source={params.get('source') ?? ''}
    </div>
  );
}
vi.mock('../src/views/Import', () => ({ default: MockImportPage }));
vi.mock('../src/views/Playlists', () => ({ default: () => <div>PLAYLISTS_PAGE</div> }));
vi.mock('../src/views/Jobs', () => ({ default: () => <div>JOBS_PAGE</div> }));
vi.mock('../src/views/Config', () => ({ default: () => <div>CONFIG_PAGE</div> }));
vi.mock('../src/views/System', () => ({ default: () => <div>SYSTEM_PAGE</div> }));
vi.mock('../src/views/Submissions', () => ({ default: () => <div>SUBMISSIONS_PAGE</div> }));
vi.mock('../src/views/Login', () => ({ default: () => <div>LOGIN_PAGE</div> }));
vi.mock('../src/components/FirstRunSetup', () => ({ default: () => <div>SETUP_WIZARD_PAGE</div> }));

// AppRoutes is auth-gated: every route decision depends on these three
// booleans (see src/App.tsx). Route-matching/navigation-destination tests
// below want the "normal" authenticated, setup-complete state so they
// exercise the same routing behavior they did before auth gating existed;
// the dedicated AppRoutes auth-gating describe block further down overrides
// them individually to cover the gating logic itself.
const AUTHENTICATED_PROPS = {
  authenticated: true,
  setupComplete: true,
  firstRunRequired: false,
};

function renderAt(path: string, props: Partial<typeof AUTHENTICATED_PROPS> = {}) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AppRoutes {...AUTHENTICATED_PROPS} {...props} />
    </MemoryRouter>,
  );
}

describe('AppRoutes (react-router v8)', () => {
  it('redirects / to /library', async () => {
    renderAt('/');
    await waitFor(() => expect(screen.getByText('LIBRARY_PAGE')).toBeTruthy());
  });

  it('redirects /clean to /jobs', async () => {
    renderAt('/clean');
    await waitFor(() => expect(screen.getByText('JOBS_PAGE')).toBeTruthy());
  });

  it('redirects /setup to /system', async () => {
    renderAt('/setup');
    await waitFor(() => expect(screen.getByText('SYSTEM_PAGE')).toBeTruthy());
  });

  it.each([
    ['/library', 'LIBRARY_PAGE'],
    ['/changes', 'CHANGES_PAGE'],
    ['/import', 'IMPORT_PAGE tab= filter= source='],
    ['/playlists', 'PLAYLISTS_PAGE'],
    ['/jobs', 'JOBS_PAGE'],
    ['/config', 'CONFIG_PAGE'],
    ['/system', 'SYSTEM_PAGE'],
    ['/submissions', 'SUBMISSIONS_PAGE'],
  ])('renders the expected page component for %s', async (path, expectedText) => {
    renderAt(path);
    await waitFor(() => expect(screen.getByText(expectedText)).toBeTruthy());
  });

  it('sends an unknown route to /library for an authenticated, set-up visitor (catch-all deep-link fallback, not a blank page)', async () => {
    renderAt('/this-route-does-not-exist');
    await waitFor(() => expect(screen.getByText('LIBRARY_PAGE')).toBeTruthy());
  });

  it('keeps query parameters available to the rendered route (dynamic/detail state in this app is query-param-based, not path-param-based)', async () => {
    renderAt('/import?tab=review&filter=unmatched&source=lidarr');
    await waitFor(() =>
      expect(screen.getByText('IMPORT_PAGE tab=review filter=unmatched source=lidarr')).toBeTruthy(),
    );
  });

  it('renders NavLink elements pointing at the expected top-level destinations', async () => {
    renderAt('/library');
    await waitFor(() => expect(screen.getByText('LIBRARY_PAGE')).toBeTruthy());
    const expected: Record<string, string> = {
      Library: '/library',
      Import: '/import',
      Playlists: '/playlists',
      Jobs: '/jobs',
    };
    for (const [label, href] of Object.entries(expected)) {
      const link = screen.getByRole('link', { name: new RegExp(`^${label}`) });
      expect(link.getAttribute('href')).toBe(href);
    }
  });

  it('marks the active NavLink for the current route', async () => {
    renderAt('/jobs');
    await waitFor(() => expect(screen.getByText('JOBS_PAGE')).toBeTruthy());
    const jobsLink = screen.getByRole('link', { name: /^Jobs/ });
    expect(jobsLink.className).toMatch(/border-red-500/);
    const libraryLink = screen.getByRole('link', { name: /^Library/ });
    expect(libraryLink.className).not.toMatch(/border-red-500/);
  });
});

describe('AppRoutes auth/setup gating', () => {
  it('sends an unauthenticated, setup-complete visitor to /login for a protected deep link', async () => {
    renderAt('/jobs', { authenticated: false, setupComplete: true, firstRunRequired: false });
    await waitFor(() => expect(screen.getByText('LOGIN_PAGE')).toBeTruthy());
  });

  it('sends a not-yet-set-up visitor to the setup wizard for any deep link, not /login', async () => {
    renderAt('/jobs', { authenticated: false, setupComplete: false, firstRunRequired: true });
    await waitFor(() => expect(screen.getByText('SETUP_WIZARD_PAGE')).toBeTruthy());
  });

  it('does not send an authenticated visitor whose setup is incomplete to /login', async () => {
    renderAt('/jobs', { authenticated: true, setupComplete: false, firstRunRequired: false });
    await waitFor(() => expect(screen.getByText('SETUP_WIZARD_PAGE')).toBeTruthy());
  });

  it('redirects an authenticated, setup-complete visitor away from /login to /library', async () => {
    renderAt('/login', { authenticated: true, setupComplete: true, firstRunRequired: false });
    await waitFor(() => expect(screen.getByText('LIBRARY_PAGE')).toBeTruthy());
  });

  it('redirects an authenticated visitor away from /login to /setup when setup is incomplete', async () => {
    renderAt('/login', { authenticated: true, setupComplete: false, firstRunRequired: false });
    await waitFor(() => expect(screen.getByText('SETUP_WIZARD_PAGE')).toBeTruthy());
  });

  it('shows the login form itself for an unauthenticated visitor at /login', async () => {
    renderAt('/login', { authenticated: false, setupComplete: true, firstRunRequired: false });
    await waitFor(() => expect(screen.getByText('LOGIN_PAGE')).toBeTruthy());
  });

  it('sends an authenticated, setup-complete visitor away from /setup to /system, not back into the wizard', async () => {
    renderAt('/setup', { authenticated: true, setupComplete: true, firstRunRequired: false });
    await waitFor(() => expect(screen.getByText('SYSTEM_PAGE')).toBeTruthy());
  });

  it('an unknown deep link during first-run setup still resolves to the setup wizard, not /login', async () => {
    renderAt('/this-route-does-not-exist', { authenticated: false, setupComplete: false, firstRunRequired: true });
    await waitFor(() => expect(screen.getByText('SETUP_WIZARD_PAGE')).toBeTruthy());
  });
});
