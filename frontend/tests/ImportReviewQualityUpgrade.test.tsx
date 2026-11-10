// Import Review rows for a qBittorrent download that is the same release as a
// library album but higher quality: informational "Upgrade available" label, no import action.
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ReviewEvidence, ReviewItem } from '../src/api/types';
import {
  BatchStopEvidence,
  QUALITY_UPGRADE_IMPORT_REASON,
  QualityUpgradeNotice,
  ReviewCard,
  isQualityUpgrade,
} from '../src/features/importReview/ImportReviewPage';

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

// Upgrade rows are informational: no import action may be enabled, Hide stays.

const RG = '11111111-2222-3333-4444-555555555555';

function renderCard(evidence?: ReviewEvidence) {
  const onApply = vi.fn();
  const onDismiss = vi.fn();
  const noop = () => undefined;
  const item = { id: 'r1', type: 'skipped', path: '/downloads/A/B', album: 'B', artist: 'A', evidence } as unknown as ReviewItem;
  render(
    <MemoryRouter>
      <ReviewCard
        item={item}
        mbid={RG}
        manualFocusToken={0}
        onMbidChange={noop}
        onUseCandidate={noop}
        onFocusManualEntry={noop}
        onValidateManualId={noop}
        onClearManualId={noop}
        onSuggest={noop}
        onApply={onApply}
        onDismiss={onDismiss}
        onDeleteFolder={noop}
        onCleanupFiles={noop}
      />
    </MemoryRouter>,
  );
  return { onApply, onDismiss };
}

describe('Import Review quality-upgrade action gating', () => {
  it('detects upgrade rows by any of the three evidence markers', () => {
    expect(isQualityUpgrade({ quality_upgrade: { release_group_id: RG } } as ReviewEvidence)).toBe(true);
    expect(isQualityUpgrade({ duplicate_rule: 'quality_upgrade' })).toBe(true);
    expect(isQualityUpgrade({ origin_step: 'quality_upgrade' })).toBe(true);
    expect(isQualityUpgrade({ origin_step: 'beets_skip' })).toBe(false);
    expect(isQualityUpgrade()).toBe(false);
  });

  it('disables Import on an upgrade row with an accessible reason and keeps Hide', () => {
    const { onApply, onDismiss } = renderCard(upgrade);
    const importButton = screen.getByRole('button', { name: 'Import with ID' }) as HTMLButtonElement;
    expect(importButton.disabled).toBe(true);
    expect(screen.getByText(QUALITY_UPGRADE_IMPORT_REASON).id).toBe(importButton.getAttribute('aria-describedby'));
    fireEvent.click(importButton);
    expect(onApply).not.toHaveBeenCalled();
    const enabledImports = screen.queryAllByRole('button')
      .filter((b) => /import|apply|repair/i.test(b.textContent || '') && !(b as HTMLButtonElement).disabled);
    expect(enabledImports).toEqual([]);
    const hide = screen.getByRole('button', { name: 'Remove from Review' }) as HTMLButtonElement;
    expect(hide.disabled).toBe(false);
    fireEvent.click(hide);
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it('leaves a normal row unchanged', () => {
    const { onApply } = renderCard({ origin_step: 'beets_skip' });
    const importButton = screen.getByRole('button', { name: 'Import with ID' }) as HTMLButtonElement;
    expect(importButton.disabled).toBe(false);
    expect(importButton.getAttribute('aria-describedby')).toBeNull();
    expect(screen.queryByText(QUALITY_UPGRADE_IMPORT_REASON)).toBeNull();
    fireEvent.click(importButton);
    expect(onApply).toHaveBeenCalledTimes(1);
  });
});
