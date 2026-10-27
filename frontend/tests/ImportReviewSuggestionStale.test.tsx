import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { SuggestionStaleBadge } from '../src/features/importReview/ImportReviewPage';
import type { ReviewItem } from '../src/api/types';

afterEach(cleanup);

const item = (over: Partial<ReviewItem>): ReviewItem =>
  ({ id: 'pending:/x', type: 'pending_ai', ...over } as ReviewItem);

describe('SuggestionStaleBadge (ARCH-034)', () => {
  it('marks a stored suggestion stale when the folder changed', () => {
    render(<SuggestionStaleBadge item={item({ suggestion_stale: true })} selectedMatchActive={false} />);
    expect(screen.getByText('Suggestion stale')).toBeTruthy();
  });

  it('shows nothing when the suggestion is not stale', () => {
    const { container } = render(
      <SuggestionStaleBadge item={item({ suggestion_stale: false })} selectedMatchActive={false} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it('hides once a fresh candidate is selected', () => {
    const { container } = render(
      <SuggestionStaleBadge item={item({ suggestion_stale: true })} selectedMatchActive={true} />,
    );
    expect(container.firstChild).toBeNull();
  });
});
