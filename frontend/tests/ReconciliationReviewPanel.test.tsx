import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ReconciliationReviewPanel } from '../src/features/importReview/ReconciliationReviewPanel';
import { getReconciliationReviews, resolveReconciliationReview } from '../src/api/client';

afterEach(cleanup);

vi.mock('../src/api/client', () => ({
  getReconciliationReviews: vi.fn(),
  resolveReconciliationReview: vi.fn(),
}));

const mockList = vi.mocked(getReconciliationReviews);
const mockResolve = vi.mocked(resolveReconciliationReview);

const side = (id: number, proof: string) => ({
  item_id: id, path: `/music/a/${id}.flac`, recording_id: '', release_id: '', release_group_id: '',
  identity_proof: proof, confidence_state: 'review_recommended', acoustid: { status: 'no_result' },
  hard_conflicts: [], review_reasons: [],
});

describe('ReconciliationReviewPanel', () => {
  it('shows backend evidence and sends the reviewer choice to the backend', async () => {
    mockList.mockResolvedValue({
      ok: true, count: 1,
      reviews: [{
        review_id: 'a'.repeat(32), kind: 'contested_slot', status: 'open', outcome: 'keep_both_review',
        existing_album_id: 10, imported_album_id: 11, release_group_id: 'rg-1', release_id: 'rel-1',
        disc: 1, track: 3, target_recording_id: 'rec-1',
        existing: side(1, 'insufficient'), imported: side(2, 'textual_support'),
        reasons: ['existing_identity_not_deterministic'], recommended_action: 'Keep both until reviewed.',
      }],
    });
    mockResolve.mockResolvedValue({ ok: true });
    render(<ReconciliationReviewPanel />);
    await screen.findByText(/Reconciliation review \(1\)/);
    expect(screen.getByText(/Identity proof: textual support/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Keep imported' }));
    await waitFor(() => expect(mockResolve).toHaveBeenCalledWith('a'.repeat(32), 'keep_imported'));
  });

  it('offers only keep-both when album identity is unproven', async () => {
    mockList.mockResolvedValue({
      ok: true, count: 1,
      reviews: [{
        review_id: 'b'.repeat(32), kind: 'album_identity_unproven', status: 'open',
        existing_album_id: 10, imported_album_id: 11, release_group_id: '', release_id: 'rel-1',
        reasons: ['existing_album_release_group_unknown'], recommended_action: '',
      }],
    });
    render(<ReconciliationReviewPanel />);
    await screen.findByText(/Reconciliation review \(1\)/);
    expect(screen.queryByRole('button', { name: 'Keep imported' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Keep both' })).toBeTruthy();
  });
});
