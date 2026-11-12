import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const src = (p: string) => readFileSync(resolve(__dirname, '../src', p), 'utf8');

// Owner decision: Submissions is the one home for mbsubmit. The Review Queue
// row keeps only its "Submit Metadata" link to that page.
describe('mbsubmit has one home', () => {
  it('the Review Queue has no inline mbsubmit dialog, only the Submissions link', () => {
    const review = src('features/importReview/ImportReviewPage.tsx');
    expect(review).not.toContain('MbsubmitDialog');
    expect(review).not.toContain('albumMbsubmit');
    expect(review).not.toContain('Prepare MB Submission');
    expect(review).toContain('navigate(`/submissions?${params.toString()}`)');
    expect(src('views/Submissions.tsx')).toContain('albumMbsubmit(activeAlbumId)');
  });
});
