// 0.7.0 Import page shell: tab changes push history (Back returns to the previous tab),
// filter changes replace it; the summary reads the Find Music cache without a rebuild.
// History tab: the AI section is hidden when no AI provider is configured.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, useLocation, useNavigationType } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('../src/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api/client')>();
  return {
    ...actual,
    apiJson: vi.fn(async () => ({ jobs: [] })),
    getAcquisitionQueue: vi.fn(async () => ({ ok: true, items: [], total: 0, counts: { beets: 0, lidarr: 0, merged: 0 } })),
    getReviewQueue: vi.fn(async () => ({ ok: true, items: [], total: 0, counts: {} })),
    getRecentImports: vi.fn(),
    getAiMatchHistory: vi.fn(),
    clearRecentImports: vi.fn(),
    getSetupStatus: vi.fn(),
  };
});

vi.mock('../src/features/acquisition/AcquisitionPanel', () => ({
  AcquisitionPanel: ({ onSourceFilterChange }: { onSourceFilterChange?: (f: string) => void }) => (
    <button type="button" onClick={() => onSourceFilterChange?.('lidarr')}>pick wanted</button>
  ),
}));
vi.mock('../src/features/intake/IntakePanel', () => ({ IntakePanel: () => <div>intake panel</div> }));
vi.mock('../src/features/importReview/ImportReviewPage', () => ({ ImportReviewPage: () => <div>review panel</div> }));

import * as client from '../src/api/client';
import { HistoryPanel } from '../src/features/history/HistoryPanel';
import Import from '../src/views/Import';

const mocked = vi.mocked(client);

function LocationProbe() {
  const location = useLocation();
  const navType = useNavigationType();
  return <div data-testid="probe">{`${navType} ${location.search}`}</div>;
}

const withQuery = (node: React.ReactNode) => (
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{node}</QueryClientProvider>
);

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('Import tabs and history', () => {
  it('tab change pushes; filter change replaces; summary never asks for a rebuild', async () => {
    render(withQuery(<MemoryRouter initialEntries={['/import']}><Import /><LocationProbe /></MemoryRouter>));
    await waitFor(() => expect(mocked.getAcquisitionQueue).toHaveBeenCalledWith({ limit: 0 }));

    fireEvent.click(screen.getByRole('tab', { name: /Find Music/ }));
    await waitFor(() => expect(screen.getByTestId('probe').textContent).toBe('PUSH ?tab=acquire'));

    fireEvent.click(await screen.findByRole('button', { name: 'pick wanted' }));
    await waitFor(() => expect(screen.getByTestId('probe').textContent).toBe('REPLACE ?tab=acquire&source=lidarr'));
  });
});

describe('History AI section', () => {
  const entry = { album: 'A', albumartist: 'B', original_path: '/x', timestamp: 1 };

  it('hidden when AI is not configured and there is no AI history', async () => {
    mocked.getRecentImports.mockResolvedValue({ imports: [entry] } as never);
    mocked.getAiMatchHistory.mockResolvedValue({ history: [] } as never);
    mocked.getSetupStatus.mockResolvedValue({ integrations: { ai: { configured: false, required: false } } } as never);
    render(withQuery(<HistoryPanel />));
    await screen.findAllByText('Recently imported');
    await waitFor(() => expect(mocked.getSetupStatus).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByText(/AI-matched imports/)).toBeNull());
  });

  it('QA N2: AI off hides past AI imports too, and never fetches them', async () => {
    mocked.getRecentImports.mockResolvedValue({ imports: [entry] } as never);
    mocked.getAiMatchHistory.mockResolvedValue({ history: [{ ...entry, album: 'Past AI album' }] } as never);
    mocked.getSetupStatus.mockResolvedValue({ integrations: { ai: { configured: false, required: false } } } as never);
    render(withQuery(<HistoryPanel />));
    await waitFor(() => expect(mocked.getSetupStatus).toHaveBeenCalled());
    await screen.findAllByText('Recently imported');
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
    expect(screen.queryByText(/AI-matched imports/)).toBeNull();
    expect(screen.queryByText(/Past AI album/)).toBeNull();
    expect(mocked.getAiMatchHistory).not.toHaveBeenCalled();
  });

  it('shown when AI is configured', async () => {
    mocked.getRecentImports.mockResolvedValue({ imports: [] } as never);
    mocked.getAiMatchHistory.mockResolvedValue({ history: [] } as never);
    mocked.getSetupStatus.mockResolvedValue({ integrations: { ai: { configured: true, required: false } } } as never);
    render(withQuery(<HistoryPanel />));
    expect(await screen.findByText(/AI-matched imports/)).toBeTruthy();
  });
});
