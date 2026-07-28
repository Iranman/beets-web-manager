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

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AppRoutes />
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

  it('renders nothing for an unknown route (existing, real fallback behavior: there is no catch-all/wildcard route, so <Routes> renders null -- not even the Shell layout)', async () => {
    const { container } = renderAt('/this-route-does-not-exist');
    await waitFor(() => expect(container.textContent).toBe(''));
    for (const marker of [
      'LIBRARY_PAGE', 'CHANGES_PAGE', 'IMPORT_PAGE', 'PLAYLISTS_PAGE',
      'JOBS_PAGE', 'CONFIG_PAGE', 'SYSTEM_PAGE', 'SUBMISSIONS_PAGE',
    ]) {
      expect(screen.queryByText(new RegExp(marker))).toBeNull();
    }
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
