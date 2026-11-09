// Import Review rows for a qBittorrent download that is the same release as a
// library album but higher quality: informational "Upgrade available" label only.
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import type { ReviewEvidence } from '../src/api/types';
import { BatchStopEvidence, QualityUpgradeNotice } from '../src/features/importReview/ImportReviewPage';

afterEach(cleanup);

const upgrade: ReviewEvidence = {
  origin_step: 'quality_upgrade',
  duplicate_rule: 'quality_upgrade',
  quality_upgrade: {
    release_group_id: '11111111-2222-3333-4444-555555555555',
    library_album_ids: [7],
    library_format: 'MP3 320 kbps',
    source_format: 'FLAC 16-bit/44.1 kHz',
  },
};

describe('Import Review quality-upgrade label', () => {
  it('shows the badge, the format line and the unchanged-library note', () => {
    render(<QualityUpgradeNotice evidence={upgrade} />);
    expect(screen.getByText('Upgrade available')).toBeTruthy();
    expect(screen.getByText('Library: MP3 320 kbps → This download: FLAC 16-bit/44.1 kHz')).toBeTruthy();
    expect(screen.getByText(/library copy is unchanged/)).toBeTruthy();
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('omits the format line when formats are missing', () => {
    render(<QualityUpgradeNotice evidence={{ origin_step: 'quality_upgrade', duplicate_rule: 'quality_upgrade' }} />);
    expect(screen.getByText('Upgrade available')).toBeTruthy();
    expect(screen.queryByText(/This download:/)).toBeNull();
  });

  it('shows no badge on other rows', () => {
    render(<QualityUpgradeNotice evidence={{ origin_step: 'beets_skip', top_candidates: [] }} />);
    expect(screen.queryByText('Upgrade available')).toBeNull();
    cleanup();
    render(<QualityUpgradeNotice />);
    expect(screen.queryByText('Upgrade available')).toBeNull();
  });

  it('is not presented as a batch-import stop', () => {
    render(<BatchStopEvidence evidence={upgrade} />);
    expect(screen.queryByText(/Batch import stopped/)).toBeNull();
  });
});
