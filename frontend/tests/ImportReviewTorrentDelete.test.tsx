// Delete Folder must not be offered on rows whose folder is a qBittorrent
// torrent (deleting would break seeding); Remove from Review stays.
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it } from 'vitest';
import type { ReviewEvidence, ReviewItem } from '../src/api/types';
import { ReviewCard, TORRENT_DELETE_REASON } from '../src/features/importReview/ImportReviewPage';

afterEach(cleanup);

function renderCard(evidence?: ReviewEvidence) {
  const noop = () => undefined;
  const item = { id: 'r1', type: 'pending_ai', path: '/downloads/A/B', album: 'B', artist: 'A', evidence } as unknown as ReviewItem;
  render(
    <MemoryRouter>
      <ReviewCard
        item={item}
        mbid=""
        manualFocusToken={0}
        onMbidChange={noop}
        onUseCandidate={noop}
        onFocusManualEntry={noop}
        onValidateManualId={noop}
        onClearManualId={noop}
        onSuggest={noop}
        onApply={noop}
        onDismiss={noop}
        onDeleteFolder={noop}
        onCleanupFiles={noop}
      />
    </MemoryRouter>,
  );
}

function expectNoDeleteButKeepRemove() {
  const enabledDeletes = screen.queryAllByRole('button')
    .filter((b) => /delete/i.test(b.textContent || '') && !(b as HTMLButtonElement).disabled);
  expect(enabledDeletes).toEqual([]);
  expect(screen.getByText(TORRENT_DELETE_REASON)).toBeTruthy();
  const remove = screen.getByRole('button', { name: 'Remove from Review' }) as HTMLButtonElement;
  expect(remove.disabled).toBe(false);
}

describe('Import Review Delete Folder on torrent rows', () => {
  it('withholds Delete Folder on a qBittorrent row', () => {
    renderCard({ source_mode: 'qbittorrent' });
    expectNoDeleteButKeepRemove();
  });

  it('withholds Delete Folder on a quality-upgrade row', () => {
    renderCard({ origin_step: 'quality_upgrade', duplicate_rule: 'quality_upgrade' });
    expectNoDeleteButKeepRemove();
  });

  it('still offers Delete Folder on a normal local row', () => {
    renderCard({ source_mode: 'local' });
    const del = screen.getByRole('button', { name: 'Delete Folder' }) as HTMLButtonElement;
    expect(del.disabled).toBe(false);
    expect(screen.queryByText(TORRENT_DELETE_REASON)).toBeNull();
  });
});
