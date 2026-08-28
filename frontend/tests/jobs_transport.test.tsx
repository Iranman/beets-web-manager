import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { describe, expect, it, vi } from 'vitest';
import { ApiError, formatTransportErrorMessage } from '../src/lib/api';
import Jobs from '../src/views/Jobs';

// Mock router / navigation
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), prefetch: vi.fn() }),
  usePathname: () => '/jobs',
  useSearchParams: () => new URLSearchParams(),
}));

function renderWithQuery(ui: React.ReactElement) {
  const testQueryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={testQueryClient}>
      <MemoryRouter>
        {ui}
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('Jobs Page API Transport Error Formatting', () => {
  it('formats network failure TypeError into friendly message', () => {
    const netErr = new TypeError('Failed to fetch');
    expect(formatTransportErrorMessage(netErr)).toBe('Could not reach Beets Web Manager.');
  });

  it('formats ApiError network category correctly', () => {
    const err = new ApiError(0, 'Could not reach Beets Web Manager.', 'network');
    expect(formatTransportErrorMessage(err)).toBe('Could not reach Beets Web Manager.');
  });

  it('formats ApiError engine_offline category correctly', () => {
    const err = new ApiError(503, 'Beets engine is unavailable.', 'engine_offline');
    expect(formatTransportErrorMessage(err)).toBe('Beets engine is unavailable.');
  });

  it('formats ApiError auth category correctly', () => {
    const err = new ApiError(401, 'Your session expired. Sign in again.', 'auth');
    expect(formatTransportErrorMessage(err)).toBe('Your session expired. Sign in again.');
  });

  it('formats ApiError timeout category correctly', () => {
    const err = new ApiError(0, 'Request timed out.', 'timeout');
    expect(formatTransportErrorMessage(err)).toBe('Request timed out.');
  });

  it('renders Jobs page error state when API fetch rejects with network error', async () => {
    vi.spyOn(global, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/jobs')) {
        return Promise.reject(new TypeError('Failed to fetch'));
      }
      return Promise.resolve(new Response(JSON.stringify({ ok: true }), { status: 200 }));
    });

    renderWithQuery(<Jobs />);

    await waitFor(() => {
      expect(screen.getAllByText('Could not reach Beets Web Manager.').length).toBeGreaterThan(0);
    });

    // Should transition out of 'Loading job activity...'
    expect(screen.queryByText('Loading job activity...')).toBeNull();
  });

  it('renders Jobs page engine offline state when backend returns 503', async () => {
    vi.spyOn(global, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/jobs')) {
        return Promise.resolve(
          new Response(
            JSON.stringify({ ok: false, error: 'Beets engine is unavailable.', error_code: 'ENGINE_OFFLINE' }),
            { status: 503, headers: { 'Content-Type': 'application/json' } },
          ),
        );
      }
      return Promise.resolve(new Response(JSON.stringify({ ok: true }), { status: 200 }));
    });

    renderWithQuery(<Jobs />);

    await waitFor(() => {
      expect(screen.getAllByText('Beets engine is unavailable.').length).toBeGreaterThan(0);
    });

    expect(screen.queryByText('Loading job activity...')).toBeNull();
  });
});
