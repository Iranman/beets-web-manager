import { useCallback, useEffect, useState } from 'react';

import { getReconciliationReviews, resolveReconciliationReview } from '../../api/client';
import type { ReconciliationChoice, ReconciliationReview, ReconciliationSide } from '../../api/types';

function label(value?: string | null): string {
  return (value || '').replace(/_/g, ' ');
}

function Side({ title, side }: { title: string; side?: ReconciliationSide }) {
  if (!side) return null;
  return (
    <div className="rounded border border-graphite-200 bg-white/80 p-2 text-xs">
      <div className="font-semibold text-zinc-900">{title} · item {side.item_id}</div>
      <div className="mt-1 break-all font-mono text-zinc-600">{side.path}</div>
      <div className="mt-1 text-zinc-700">
        Identity proof: {label(side.identity_proof)} · Confidence: {label(side.confidence_state)} · AcoustID:{' '}
        {label(side.acoustid?.status)}
      </div>
      {side.recording_id ? <div className="font-mono text-zinc-600">Recording {side.recording_id}</div> : null}
      {side.hard_conflicts?.length ? (
        <div className="mt-1 text-rose-800">Hard conflicts: {side.hard_conflicts.map(label).join(', ')}</div>
      ) : null}
    </div>
  );
}

/**
 * Import reconciliation decisions the backend refused to make automatically.
 * Both files and both library rows were kept; the reviewer chooses. The
 * backend validates and executes the choice through engine transactions.
 */
export function ReconciliationReviewPanel() {
  const [reviews, setReviews] = useState<ReconciliationReview[]>([]);
  const [busy, setBusy] = useState<string>('');
  const [error, setError] = useState<string>('');

  const load = useCallback(async () => {
    try {
      const res = await getReconciliationReviews();
      setReviews(res.reviews || []);
      setError('');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load reconciliation reviews.');
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const resolve = async (review: ReconciliationReview, choice: ReconciliationChoice) => {
    setBusy(review.review_id);
    try {
      await resolveReconciliationReview(review.review_id, choice);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not apply the decision.');
    } finally {
      setBusy('');
    }
  };

  if (!reviews.length && !error) return null;
  return (
    <section className="mb-4 rounded border border-amber-200 bg-amber-50/70 p-3 text-sm" aria-label="Reconciliation review">
      <div className="font-semibold text-amber-900">Reconciliation review ({reviews.length})</div>
      <div className="text-xs text-amber-900">
        These imports landed on a track that already exists. Without deterministic evidence both files were kept.
      </div>
      {error ? <div className="mt-2 text-rose-800">{error}</div> : null}
      <ul className="mt-2 space-y-3">
        {reviews.map((review) => {
          const slotReview = review.kind === 'contested_slot';
          return (
            <li key={review.review_id} className="rounded border border-amber-200 bg-white/70 p-2">
              <div className="text-xs text-zinc-800">
                {slotReview ? `Disc ${review.disc ?? 1} · Track ${review.track ?? '?'} · ` : ''}
                {label(review.outcome || review.kind)} · Release Group {review.release_group_id || 'unknown'}
              </div>
              <div className="text-xs text-zinc-700">Reasons: {review.reasons.map(label).join(', ')}</div>
              {review.recommended_action ? <div className="text-xs text-zinc-700">{review.recommended_action}</div> : null}
              {slotReview ? (
                <div className="mt-2 grid gap-2 md:grid-cols-2">
                  <Side title="Existing" side={review.existing} />
                  <Side title="Imported" side={review.imported} />
                </div>
              ) : null}
              <div className="mt-2 flex flex-wrap gap-2">
                {slotReview ? (
                  <>
                    <button type="button" className="rounded border px-2 py-1 text-xs" disabled={busy === review.review_id}
                      onClick={() => void resolve(review, 'keep_existing')}>Keep existing</button>
                    <button type="button" className="rounded border px-2 py-1 text-xs" disabled={busy === review.review_id}
                      onClick={() => void resolve(review, 'keep_imported')}>Keep imported</button>
                  </>
                ) : null}
                <button type="button" className="rounded border px-2 py-1 text-xs" disabled={busy === review.review_id}
                  onClick={() => void resolve(review, 'keep_both')}>Keep both</button>
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
