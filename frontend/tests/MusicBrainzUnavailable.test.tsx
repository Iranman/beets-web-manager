import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { suggestItem } from '../src/api/client';
import { apiPost } from '../src/lib/api';
import { MUSICBRAINZ_UNAVAILABLE_TEXT } from '../src/components/MusicBrainzUnavailableNotice';
import { ReviewCard } from '../src/features/importReview/ImportReviewPage';
import type { AiSuggestResponse, ReviewItem } from '../src/api/types';

const noop = () => undefined;

function renderCard(suggestion?: AiSuggestResponse) {
  const item = { id: 'r1', type: 'pending_ai', path: '/downloads/Artist/Album' } as ReviewItem;
  render(
    <MemoryRouter>
      <ReviewCard
        item={item}
        mbid=""
        suggestion={suggestion}
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

describe('musicbrainz_unavailable notice (#252 NF-4)', () => {
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it('shows a polite status notice when the ai-suggest response sets the flag', () => {
    renderCard({ ok: true, musicbrainz_unavailable: true });
    const notice = screen.getByRole('status');
    expect(notice.textContent).toContain(MUSICBRAINZ_UNAVAILABLE_TEXT);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('shows nothing when MusicBrainz was reachable', () => {
    renderCard({ ok: true, musicbrainz_unavailable: false });
    expect(screen.queryByText(MUSICBRAINZ_UNAVAILABLE_TEXT)).toBeNull();
  });
});

describe('provider 503 {ok:false, error, unavailable:true}', () => {
  afterEach(() => vi.unstubAllGlobals());

  const stub503 = (body: Record<string, unknown>) =>
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(JSON.stringify(body), { status: 503 }))));

  it('api/client keeps the fixed provider text instead of "Beets engine is unavailable."', async () => {
    stub503({ ok: false, error: 'MusicBrainz is unavailable. Try again later.', unavailable: true });
    await expect(suggestItem(1)).rejects.toThrow('MusicBrainz is unavailable. Try again later.');
  });

  it('lib/api keeps the fixed provider text', async () => {
    stub503({ ok: false, error: 'MusicBrainz is unavailable. Try again later.', unavailable: true });
    await expect(apiPost('/api/folders/ai-suggest', { path: 'x' })).rejects.toThrow('MusicBrainz is unavailable. Try again later.');
  });

  it('an uncoded 503 without the flag is still reported as an engine outage', async () => {
    stub503({ ok: false, error: 'boom' });
    await expect(suggestItem(1)).rejects.toThrow('Beets engine is unavailable.');
  });
});
