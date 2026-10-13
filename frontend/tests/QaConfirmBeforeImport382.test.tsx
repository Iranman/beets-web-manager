// QA #382: gaps not covered by the PR's own tests.
// Import All must not start until "Import eligible only" is clicked in its dialog;
// reconciliation Keep existing must send keep_existing after confirm.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('../src/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api/client')>();
  return {
    ...actual,
    getAiBatchStatus: vi.fn(),
    getSetupStatus: vi.fn(),
    runPreflight: vi.fn(),
    startAiBatchImport: vi.fn(),
    getReconciliationReviews: vi.fn(),
    resolveReconciliationReview: vi.fn(),
  };
});

import * as client from '../src/api/client';
import { ReconciliationReviewPanel } from '../src/features/importReview/ReconciliationReviewPanel';
import { IntakePanel } from '../src/features/intake/IntakePanel';

const mocked = vi.mocked(client);
const ROOT = '/srv/dl';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.restoreAllMocks();
  localStorage.clear();
});

describe('Import All confirms first', () => {
  it('nothing on open or Cancel; startAiBatchImport(path) only after "Import eligible only"', async () => {
    mocked.getAiBatchStatus.mockResolvedValue({ state: { status: 'idle' } } as never);
    mocked.getSetupStatus.mockResolvedValue({ paths: { downloads: { path: ROOT, exists: true } } } as never);
    mocked.runPreflight.mockResolvedValue({
      ok: true, path: ROOT, folders: [{ path: `${ROOT}/A`, already_in_library: false }], audio_folders: 1, audio_files: 3,
      already_in_library_folders: 0, pending_review: 0, unsupported_files: 0, empty_dirs: 0, artist_folder_groups: 0,
    } as never);
    mocked.startAiBatchImport.mockResolvedValue({ ok: true, job_id: 'j1', state: { status: 'running', job_id: 'j1' } } as never);
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter><IntakePanel /></MemoryRouter>
      </QueryClientProvider>,
    );
    await screen.findByDisplayValue(ROOT);
    fireEvent.click(screen.getByRole('button', { name: 'Preview Import All' }));
    await screen.findByText(/1 new folder eligible for Import All/);

    fireEvent.click(screen.getAllByRole('button', { name: 'Import All' })[0]);
    let dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/write tags and move imported files/)).toBeTruthy();
    expect(mocked.startAiBatchImport).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await act(async () => { await new Promise((r) => setTimeout(r, 50)); });
    expect(mocked.startAiBatchImport).not.toHaveBeenCalled();

    fireEvent.click(screen.getAllByRole('button', { name: 'Import All' })[0]);
    dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Import eligible only' }));
    await waitFor(() => expect(mocked.startAiBatchImport).toHaveBeenCalledTimes(1));
    expect(mocked.startAiBatchImport).toHaveBeenCalledWith(ROOT);
  });
});

describe('Reconciliation Keep existing confirms first', () => {
  it('nothing on Cancel; keep_existing after confirm', async () => {
    const side = (id: number) => ({
      item_id: id, path: `/music/a/${id}.flac`, recording_id: '', release_id: '', release_group_id: '',
      identity_proof: 'insufficient', confidence_state: 'review_recommended', acoustid: { status: 'no_result' },
      hard_conflicts: [], review_reasons: [],
    });
    mocked.getReconciliationReviews.mockResolvedValue({
      ok: true, count: 1,
      reviews: [{
        review_id: 'e'.repeat(32), kind: 'contested_slot', status: 'open', outcome: 'keep_both_review',
        existing_album_id: 10, imported_album_id: 11, release_group_id: 'rg-1', release_id: 'rel-1',
        disc: 1, track: 3, target_recording_id: 'rec-1', existing: side(1), imported: side(2),
        reasons: [], recommended_action: '',
      }],
    } as never);
    mocked.resolveReconciliationReview.mockResolvedValue({ ok: true } as never);
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    render(<ReconciliationReviewPanel />);
    const button = await screen.findByRole('button', { name: 'Keep existing' });

    fireEvent.click(button);
    expect(confirmSpy).toHaveBeenCalledWith(expect.stringMatching(/quarantine/));
    await act(async () => { await new Promise((r) => setTimeout(r, 20)); });
    expect(mocked.resolveReconciliationReview).not.toHaveBeenCalled();

    confirmSpy.mockReturnValue(true);
    fireEvent.click(button);
    await waitFor(() => expect(mocked.resolveReconciliationReview).toHaveBeenCalledWith('e'.repeat(32), 'keep_existing'));
  });
});
