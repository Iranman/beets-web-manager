/**
 * #584: identity_validated null means the AI artist gate could not compare
 * artists. The AI match must keep it unresolved (not true), stay usable for a
 * human reviewer (null is not a rejection), and the target preview key must
 * tell it apart from a proven identity.
 */
import { describe, expect, it } from 'vitest';
import type { AiSuggestion } from '../src/api/types';
import { buildAiSelectedMatch } from '../src/features/importReview/ImportReviewPage';

const RG = '11111111-2222-3333-4444-555555555555';
const REL = '66666666-7777-8888-9999-aaaaaaaaaaaa';
const base = {
  album: 'B',
  albumartist: 'A',
  mb_albumid: REL,
  mb_releasegroupid: RG,
  preflight: { ok: true, matches: 1, expected: 1, audio_count: 1 },
  track_mapping: [{ num: 1, local_title: 'a', mb_title: 'A', mb_trackid: '', status: 'matched', source_path: '/m/01.flac' }],
} as unknown as AiSuggestion;

describe('AI match keeps an unresolved artist identity (#584)', () => {
  it('null stays null and is not blocked', () => {
    const match = buildAiSelectedMatch({ ...base, identity_validated: null });
    expect(match.identity_validated).toBeNull();
    expect(match.is_importable).toBe(true);
  });

  it('true and false pass through', () => {
    expect(buildAiSelectedMatch({ ...base, identity_validated: true }).identity_validated).toBe(true);
    const rejected = buildAiSelectedMatch({ ...base, identity_validated: false });
    expect(rejected.identity_validated).toBe(false);
    expect(rejected.is_importable).toBe(false);
  });
});
