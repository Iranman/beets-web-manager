/**
 * ARCH-005 contract: the page's decision rules
 * (src/features/importReview/importReviewDecision.ts) must return exactly
 * what the backend authority (backend/import_review_decision.py) returns for
 * the same input. The cases and their expected payloads are generated from
 * the backend (tests/fixtures/build_import_review_decision_cases.py) and the
 * backend checks them too (tests/test_import_review_decision.py), so a rule
 * changed on one side only fails one of the two suites.
 */
import { describe, expect, it } from 'vitest';
import cases from './fixtures/import_review_decision_cases.json';
import {
  decideImportReview,
  type ImportReviewDecision,
  type ImportReviewDecisionEntry,
} from '../src/features/importReview/importReviewDecision';

type Case = { name: string; input: unknown; expected: ImportReviewDecision };

describe('Import Review decision contract (frontend mirror === backend authority)', () => {
  const all = cases as Case[];

  it('has a meaningful number of shared cases', () => {
    expect(all.length).toBeGreaterThanOrEqual(40);
  });

  it.each(all.map((entry) => [entry.name, entry] as const))('%s', (_name, entry) => {
    expect(decideImportReview(entry.input as ImportReviewDecisionEntry)).toEqual(entry.expected);
  });
});
