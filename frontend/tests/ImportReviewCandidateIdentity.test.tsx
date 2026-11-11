/**
 * #596: the candidate track comparison proves Release Group membership, not
 * the artist. A candidate match is proven only when the suggestion proved the
 * artist for the same candidate; otherwise it stays unresolved (null), is
 * importable by hand, and is never auto-import eligible.
 */
import { describe, expect, it } from 'vitest';
import type { AiSuggestion, ReviewCandidate } from '../src/api/types';
import { buildAiSelectedMatch, buildCandidateSelectedMatch } from '../src/features/importReview/ImportReviewPage';

const RG = '11111111-2222-3333-4444-555555555555';
const REL = '66666666-7777-8888-9999-aaaaaaaaaaaa';
const candidate = { mb_albumid: REL, mb_releasegroupid: RG, match_total: 0.95, tracks: 1 } as unknown as ReviewCandidate;
const trackData = {
  selected_release_group_id: RG,
  representative_release_id: REL,
  identity_validated: true,
  mb_track_count: 1,
  local_track_count: 1,
  matched_count: 1,
  comparison: [{ num: 1, local_title: 'a', mb_title: 'A', mb_trackid: '', status: 'matched', source_path: '/m/01.flac' }],
  preflight: { ok: true },
} as unknown as Parameters<typeof buildCandidateSelectedMatch>[1];

const build = (suggestion?: Record<string, unknown>) =>
  buildCandidateSelectedMatch(candidate, trackData, undefined, '', suggestion);

describe('candidate match keeps an unresolved artist identity (#596)', () => {
  it('unresolved suggestion stays null: importable by hand, not auto', () => {
    for (const suggestion of [undefined, { identity_validated: null, mb_releasegroupid: RG }]) {
      const match = build(suggestion);
      expect(match.identity_validated).toBeNull();
      expect(match.is_importable).toBe(true);
      expect(match.auto_fix_eligible).toBe(false);
      expect(match.auto_fix_reason).not.toMatch(/Auto-import eligible/);
    }
  });

  it('a proven identity for another candidate does not carry over', () => {
    const other = '99999999-2222-3333-4444-555555555555';
    expect(build({ identity_validated: true, mb_releasegroupid: other }).identity_validated).toBeNull();
  });

  it('a proven identity for this candidate is auto eligible', () => {
    const match = build({ identity_validated: true, mb_releasegroupid: RG });
    expect(match.identity_validated).toBe(true);
    expect(match.auto_fix_eligible).toBe(true);
  });
});

describe('AI match with an unresolved identity is not auto eligible (#596)', () => {
  const suggestion = {
    album: 'B', albumartist: 'A', mb_albumid: REL, mb_releasegroupid: RG, confidence_score: 0.95,
    preflight: { ok: true, matches: 1, expected: 1, audio_count: 1 },
    track_mapping: [{ num: 1, local_title: 'a', mb_title: 'A', mb_trackid: '', status: 'matched', source_path: '/m/01.flac' }],
  } as unknown as AiSuggestion;

  it('null is importable by hand but not auto eligible', () => {
    const match = buildAiSelectedMatch({ ...suggestion, identity_validated: null });
    expect(match.is_importable).toBe(true);
    expect(match.auto_fix_eligible).toBe(false);
    expect(match.auto_fix_reason).not.toMatch(/Auto-import eligible/);
  });

  it('true is auto eligible', () => {
    const match = buildAiSelectedMatch({ ...suggestion, identity_validated: true });
    expect(match.auto_fix_eligible).toBe(true);
    expect(match.auto_fix_reason).toMatch(/Auto-import eligible/);
  });
});
