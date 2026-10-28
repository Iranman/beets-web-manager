// ARCH-030 C8: the recovery card's single primary action must SUBMIT the
// carrier identity it displays (canonical Release Group + edition + destination
// album) — never the editable page-level mbid / selectedMatch, which may be an
// unrelated title-collision candidate. The RecoveryReviewCard unit tests stub
// onPrimaryAction and never assert the payload; this exercises the real
// ImportReviewPage wiring (card button -> confirm dialog -> runApply -> backend
// call) and asserts the payload.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ReactNode } from 'react';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { RecoveryCarrierIdentity, ReviewItem } from '../src/api/types';

vi.mock('../src/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/api/client')>();
  return {
    ...actual,
    getReviewQueue: vi.fn(),
    previewImportTarget: vi.fn(),
    decideImportReviewRemote: vi.fn(),
    reimportDisk: vi.fn(),
    importWithId: vi.fn(),
    getJob: vi.fn(),
    getSetupStatus: vi.fn(),
  };
});

vi.mock('../src/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/lib/api')>();
  return { ...actual, apiGet: vi.fn(() => new Promise(() => undefined)) };
});

import * as client from '../src/api/client';
import { ImportReviewPage } from '../src/features/importReview/ImportReviewPage';

const mocked = vi.mocked(client);

// Canonical carrier destination (album 112, "Forever").
const TARGET_RG = '11111111-1111-4111-8111-111111111111';
const TARGET_RELEASE = '44444444-4444-4444-8444-444444444444';
// An unrelated title-collision id sitting in the editable page mbid field.
const FLEUR_RELEASE = '99999999-9999-4999-8999-999999999999';

function wrap(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

async function settle() {
  await act(async () => { await new Promise((r) => setTimeout(r, 50)); });
}

function carrier(overrides: Partial<RecoveryCarrierIdentity> = {}): RecoveryCarrierIdentity {
  return {
    recovery: true,
    recovery_carrier_id: 'carrier-1',
    recovery_identity_key: 'key-1',
    existing_album_id: 112,
    mb_releasegroupid: TARGET_RG,
    release_id: TARGET_RELEASE,
    destination_album_path: '/music/112/Forever',
    wanted_tracks: [],
    expected_tracks: [{ recording_id: '22222222-2222-4222-8222-222222222222', track: 7, disc: 1, title: 'Only You' }],
    tracks: [{ recording_id: '22222222-2222-4222-8222-222222222222', title: 'Only You', position: 7, disc: 1, status: 'pending' }],
    confidence: 'review_required',
    review_reason: 'Downloaded recording not verified against the expected track.',
    // Manual path: auto-import is NOT allowed, so the human confirm is used.
    matching_decision: {
      action_allowed: false,
      identity_proof: 'release_group_id',
      recommended_action: 'Verify the download before importing.',
      dimensions: {},
    },
    ...overrides,
  };
}

// A carrier-backed recovery item whose EDITABLE page mbid (item.mb_albumid, the
// initialMbid default) is an unrelated FLEUR release — the C8 divergence source.
// It deliberately carries NO top-level existing_album_id, to prove the apply
// reads the destination album from the carrier packet, not the item.
function recoveryItem(c: RecoveryCarrierIdentity = carrier()): ReviewItem {
  return {
    id: 'r1',
    type: 'pending_ai',
    path: '/downloads/112 - Forever',
    album: 'Forever',
    artist: '112',
    mb_albumid: FLEUR_RELEASE,
    evidence: { recovery_carrier: c },
  } as unknown as ReviewItem;
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(JSON.stringify({ ok: true, items: [] }), { status: 200 }))));
  mocked.getReviewQueue.mockResolvedValue({
    ok: true,
    items: [recoveryItem()],
    counts: { all: 1, pending_ai: 1 },
  } as never);
  mocked.previewImportTarget.mockReturnValue(new Promise(() => undefined) as never);
  mocked.getSetupStatus.mockResolvedValue({ paths: { downloads: { path: '/srv/dl', exists: true } } } as never);
  mocked.decideImportReviewRemote.mockResolvedValue({ ok: true, decisions: [{ can_apply: true }] } as never);
  mocked.reimportDisk.mockResolvedValue({ ok: true, job_id: 'job-1' } as never);
  mocked.importWithId.mockResolvedValue({ ok: true, job_id: 'job-2' } as never);
  // Keep the post-start poll harmless (never resolves) — the assertion is the
  // backend call payload, not job completion.
  mocked.getJob.mockReturnValue(new Promise(() => undefined) as never);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  localStorage.clear();
});

describe('ARCH-030 C8: recovery primary action submits the carrier identity it displays', () => {
  it('submits the carrier RG/edition/album, NOT the editable page mbid, through the human-review confirm', async () => {
    wrap(<ImportReviewPage />);
    await screen.findByLabelText('Missing-track recovery review');
    await settle();

    // The single primary action is the human-review manual confirm (auto not allowed).
    const primary = screen.getByTestId('recovery-primary-action') as HTMLButtonElement;
    expect(primary.getAttribute('data-recovery-action-mode')).toBe('manual');
    expect(primary.disabled).toBe(false); // not blocked by the editable-mbid gate
    fireEvent.click(primary);

    // Confirm dialog shows the carrier canonical Release Group, never the FLEUR id.
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(TARGET_RG)).toBeTruthy();
    expect(within(dialog).queryByText(FLEUR_RELEASE)).toBeNull();
    expect(mocked.reimportDisk).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm' }));

    // The human Review import path is used (reimportDisk / repair into the
    // existing album), carrying the carrier identity — NOT importWithId, and
    // NOT the page-level FLEUR release.
    await waitFor(() => expect(mocked.reimportDisk).toHaveBeenCalledTimes(1));
    expect(mocked.importWithId).not.toHaveBeenCalled();
    const payload = mocked.reimportDisk.mock.calls[0][0];
    expect(payload.mb_albumid).toBe(TARGET_RELEASE);
    expect(payload.mb_albumid).not.toBe(FLEUR_RELEASE);
    // The destination album comes from the carrier packet (item has none).
    expect(payload.existing_album_id).toBe(112);
    expect(payload.aldir).toBe('/downloads/112 - Forever');
    // No recovery_carrier_id is sent: this stays the human-authorized Review
    // import, not the unattended auto-gated advancer.
    expect((payload as Record<string, unknown>).recovery_carrier_id).toBeUndefined();
  });

  it('falls back to the carrier Release Group as mb_albumid when the carrier has no edition', async () => {
    mocked.getReviewQueue.mockResolvedValue({
      ok: true,
      items: [recoveryItem(carrier({ release_id: '' }))],
      counts: { all: 1, pending_ai: 1 },
    } as never);
    wrap(<ImportReviewPage />);
    await screen.findByLabelText('Missing-track recovery review');
    await settle();

    fireEvent.click(screen.getByTestId('recovery-primary-action'));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm' }));

    await waitFor(() => expect(mocked.reimportDisk).toHaveBeenCalledTimes(1));
    const payload = mocked.reimportDisk.mock.calls[0][0];
    expect(payload.mb_albumid).toBe(TARGET_RG);
    expect(payload.existing_album_id).toBe(112);
  });

  it('validates the carrier identity with the backend (decision carries the carrier RG, not the page mbid)', async () => {
    wrap(<ImportReviewPage />);
    await screen.findByLabelText('Missing-track recovery review');
    await settle();

    fireEvent.click(screen.getByTestId('recovery-primary-action'));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Confirm' }));

    await waitFor(() => expect(mocked.decideImportReviewRemote).toHaveBeenCalledTimes(1));
    const entries = mocked.decideImportReviewRemote.mock.calls[0][0] as Array<{ mbid: string; selected_match: unknown }>;
    expect(entries[0].mbid).toBe(TARGET_RG);
    expect(entries[0].mbid).not.toBe(FLEUR_RELEASE);
    // No page-selected match is carried for a carrier item.
    expect(entries[0].selected_match).toBeNull();
  });
});
