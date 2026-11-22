/**
 * PR4/Lane D: the ranked alternative-candidates panel for a pending_ai Review
 * row. It renders the PR3 `item.alt_candidates` projection
 * (serialize_review_alt_candidates) verbatim in the given deterministic order:
 *
 *  - the folder-level auto-import verdict ("why" it was / was not auto-imported);
 *  - the BEST VERIFIED candidate first, then every ranked ALTERNATIVE (as given,
 *    never re-sorted on the frontend);
 *  - for each candidate a clear SUGGESTION vs VALIDATED distinction (text, not
 *    colour alone), verified track coverage, sources, edition match, the review
 *    "why", clickable MusicBrainz release + release-group links, and the stored
 *    per-track matched/failed/why under a keyboard-accessible disclosure.
 *
 * It owns no fetch and no identity logic: "Use this release" routes the chosen
 * Release Group ID back through the existing manual-MBID / selected-match flow
 * (onUseCandidate), which validates and imports on the normal pipeline. The
 * live, authoritative per-track view for the selected release stays the
 * CandidateCarousel's GET /api/candidates/<id>/tracks path.
 */
import { MbidLink } from './MbidLink';
import { sameMbid } from './importReviewDecision';
import type { ReviewAltCandidate, ReviewAltCandidatePerTrack, ReviewItem } from '../../api/types';

function pct(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return '';
  return `${Math.round(value * 100)}%`;
}

/** The suggestion-vs-validated label for one candidate. Text-first so screen
 * readers announce the distinction, never colour alone. */
function candidateStatus(candidate: ReviewAltCandidate): { label: string; tone: string; description: string } {
  if (candidate.is_best_verified) {
    return {
      label: 'Best verified match',
      tone: 'border-emerald-500/40 bg-emerald-950/40 text-emerald-200',
      description: 'Verified and importable. This is the best candidate the deterministic matcher validated.',
    };
  }
  if (candidate.action_allowed) {
    return {
      label: 'Validated',
      tone: 'border-emerald-500/30 bg-emerald-950/30 text-emerald-200',
      description: 'Verified and importable.',
    };
  }
  return {
    label: 'Suggestion',
    tone: 'border-amber-500/30 bg-amber-950/40 text-amber-200',
    description: 'A suggestion that was not validated. Review the reasons below before importing.',
  };
}

function PerTrackTable({ rows }: { rows: ReviewAltCandidatePerTrack[] }) {
  if (!rows.length) return <p className="px-3 py-2 text-xs text-muted">No per-track detail was stored for this candidate.</p>;
  return (
    <table className="w-full border-collapse text-left text-xs">
      <thead>
        <tr className="text-muted">
          <th scope="col" className="px-2 py-1 font-medium">#</th>
          <th scope="col" className="px-2 py-1 font-medium">Your file</th>
          <th scope="col" className="px-2 py-1 font-medium">MusicBrainz track</th>
          <th scope="col" className="px-2 py-1 font-medium">Result</th>
          <th scope="col" className="px-2 py-1 font-medium">Why</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row, idx) => {
          const matched = row.status === 'matched' || row.status === 'fuzzy' || row.status === 'verified_match' || row.status === 'acoustid_verified';
          const resultTone = matched ? 'text-emerald-300' : row.status === 'missing' ? 'text-amber-300' : 'text-rose-300';
          return (
            <tr key={`${row.position ?? 'u'}-${row.local_title}-${idx}`} className="border-t border-graphite-800 align-top">
              <td className="px-2 py-1 text-muted">{row.position ?? '—'}</td>
              <td className="px-2 py-1 text-graphite-100">{row.local_title || <span className="text-muted">—</span>}</td>
              <td className="px-2 py-1 text-graphite-100">{row.mb_title || <span className="text-muted">—</span>}</td>
              <td className={`px-2 py-1 font-medium ${resultTone}`}>
                {matched ? 'matched' : row.status.replace(/_/g, ' ')}
                {row.acoustid_status ? <span className="ml-1 text-muted">({row.acoustid_status})</span> : null}
              </td>
              <td className="px-2 py-1 text-muted">{row.reason || (matched ? '' : '—')}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function CandidateRow({
  candidate,
  index,
  selectedRgid,
  onUseCandidate,
}: {
  candidate: ReviewAltCandidate;
  index: number;
  selectedRgid?: string;
  onUseCandidate: (mbid: string) => void;
}) {
  const status = candidateStatus(candidate);
  const inUse = sameMbid(selectedRgid, candidate.release_group_id)
    || sameMbid(selectedRgid, candidate.representative_release_id);
  const scorePct = pct(candidate.score);
  const { local_tracks_verified: verified, local_tracks_total: totalTracks } = candidate.coverage;
  const reasons = candidate.review_reasons.length ? candidate.review_reasons : candidate.conflicts;
  const headingId = `alt-candidate-${candidate.release_group_id || index}`;
  const perTrack = candidate.per_track ?? [];

  return (
    <li
      data-testid="alt-candidate"
      className={`rounded border p-2.5 ${candidate.is_best_verified ? 'border-emerald-500/40 bg-emerald-950/20' : 'border-graphite-700 bg-graphite-900'}`}
      aria-current={inUse ? 'true' : undefined}
    >
      {/* Compact decision row: the badge, rank, match, coverage and the action
          on one scannable line. All supporting detail (IDs, evidence, edition,
          reasons, per-track) lives in the "Details" disclosure below so the
          panel leads with the decision, not the full dossier for every edition. */}
      <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5">
        <span className={`rounded-md border px-2 py-0.5 text-xs font-semibold ${status.tone}`}>{status.label}</span>
        <span className="text-xs text-muted">Rank {candidate.deterministic_rank || index + 1}</span>
        {scorePct ? <span className="text-xs font-semibold text-graphite-100">{scorePct} match</span> : null}
        <span className="text-xs text-muted">
          {verified}/{totalTracks} track{totalTracks === 1 ? '' : 's'} verified
        </span>
        {inUse ? <span className="text-xs font-semibold text-emerald-300">Selected</span> : null}
        <button
          type="button"
          className="ml-auto rounded border border-graphite-600 px-2.5 py-1 text-xs font-semibold text-graphite-100 transition-colors hover:bg-graphite-800 disabled:cursor-not-allowed disabled:opacity-40"
          onClick={() => onUseCandidate(candidate.release_group_id)}
          disabled={!candidate.release_group_id || inUse}
          aria-label={inUse ? `${status.label} — selected` : `Use this release — ${status.label}`}
          aria-describedby={headingId}
        >
          {inUse ? 'Selected' : 'Use this release'}
        </button>
      </div>

      <details className="mt-2 rounded border border-graphite-800 bg-graphite-950/40">
        <summary className="cursor-pointer rounded px-2.5 py-1.5 text-xs font-medium text-graphite-200 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-300">
          Details
        </summary>
        <div className="space-y-1.5 px-2.5 pb-2.5 pt-0.5">
          <p id={headingId} className="text-xs text-muted">{status.description}</p>

          {candidate.sources.length ? (
            <div className="text-xs text-muted">Evidence: {candidate.sources.join(', ')}</div>
          ) : null}
          {candidate.edition_match.length ? (
            <div className="text-xs text-muted">Edition: {candidate.edition_match.join(', ')}</div>
          ) : null}

          {reasons.length ? (
            <div className="rounded border border-amber-500/30 bg-amber-950/30 px-2 py-1.5 text-xs text-amber-200">
              <div className="font-semibold">Why this needs review</div>
              <ul className="mt-0.5 list-disc space-y-0.5 pl-4">
                {reasons.map((reason) => <li key={reason}>{reason}</li>)}
              </ul>
            </div>
          ) : null}

          <div className="space-y-1">
            {candidate.mb_release_group_url ? (
              <MbidLink entity="release-group" id={candidate.release_group_id} status={candidate.action_allowed ? 'verified' : 'suggested'} label="Release Group" />
            ) : null}
            {candidate.mb_release_url && candidate.representative_release_id ? (
              <MbidLink entity="release" id={candidate.representative_release_id} status="suggested" label="Release (edition)" />
            ) : null}
          </div>

          {perTrack.length ? (
            <details className="rounded border border-graphite-800 bg-graphite-950/60">
              <summary className="cursor-pointer px-2.5 py-1.5 text-xs font-medium text-graphite-200">
                Per-track detail ({perTrack.length})
              </summary>
              <div className="overflow-x-auto">
                <PerTrackTable rows={perTrack} />
              </div>
            </details>
          ) : null}
        </div>
      </details>
    </li>
  );
}

export function AltCandidatesPanel({
  item,
  selectedRgid,
  onUseCandidate,
}: {
  item: ReviewItem;
  selectedRgid?: string;
  onUseCandidate: (mbid: string) => void;
}) {
  const alt = item.alt_candidates;
  if (!alt || !alt.candidates.length) return null;

  const verdict = alt.auto_import;
  // The folder-level "why": an explicit refusal (eligible === false) is the
  // authoritative verdict driving readiness, so surface its reasons here.
  const refused = verdict.eligible === false;
  const verified = verdict.eligible === true;

  // Progressive disclosure (Operate mode): the primary decision — the first
  // ranked candidate, which is the best-verified match when one exists — stays
  // open. The remaining alternatives keep every field but collapse into one
  // disclosure so the page leads with the decision, not the full ranked list.
  // Order is preserved verbatim (never re-sorted): primary is candidates[0],
  // the rest follow in given order. The disclosure opens automatically when the
  // currently selected release is one of the alternatives, so a prior choice is
  // never hidden.
  const [primary, ...rest] = alt.candidates;
  const restSelected = rest.some(
    (candidate) =>
      sameMbid(selectedRgid, candidate.release_group_id) || sameMbid(selectedRgid, candidate.representative_release_id),
  );

  return (
    <section className="mt-3 rounded border border-graphite-700 bg-graphite-900/60 p-3" aria-label="Ranked MusicBrainz candidates">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold text-graphite-50">Ranked MusicBrainz candidates</h3>
        <span
          className={`rounded-md border px-2 py-0.5 text-xs font-semibold ${
            verified
              ? 'border-emerald-500/30 bg-emerald-950/40 text-emerald-200'
              : refused
                ? 'border-amber-500/30 bg-amber-950/40 text-amber-200'
                : 'border-graphite-700 bg-graphite-850 text-graphite-200'
          }`}
        >
          {verified ? 'Auto-import eligible' : refused ? 'Needs decision' : 'Review'}
        </span>
      </div>

      {/* When a verified candidate exists, the "Best verified match" row below
          plus the "Needs decision" badge already carry the message, and the
          review card states it once — so the panel does not repeat it. Only the
          no-verified case needs its own line here, since nothing else says it. */}
      {refused && !alt.best_verified_release_group_id ? (
        <p className="mt-1.5 text-xs text-amber-200">
          Auto-import was refused: the best candidate could not be evaluated. Choose a release below or enter a MusicBrainz ID.
        </p>
      ) : null}

      {verdict.reasons.length ? (
        <details className="mt-1.5 text-xs text-muted">
          <summary className="cursor-pointer rounded font-medium text-graphite-200 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-300">
            Why auto-import was refused ({verdict.reasons.length})
          </summary>
          <ul className="mt-1 list-disc space-y-0.5 pl-4">
            {verdict.reasons.map((reason) => <li key={reason}>{reason}</li>)}
          </ul>
        </details>
      ) : null}

      <ul className="mt-2.5 space-y-2.5">
        {primary ? (
          <CandidateRow
            key={primary.release_group_id || 0}
            candidate={primary}
            index={0}
            selectedRgid={selectedRgid}
            onUseCandidate={onUseCandidate}
          />
        ) : null}
      </ul>

      {rest.length ? (
        <details className="group mt-2.5 rounded border border-graphite-800 bg-graphite-950/40" open={restSelected || undefined}>
          <summary className="cursor-pointer rounded px-3 py-2 text-xs font-semibold text-graphite-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-300">
            <span className="group-open:hidden">Show {rest.length} other candidate{rest.length === 1 ? '' : 's'}</span>
            <span className="hidden group-open:inline">Hide {rest.length} other candidate{rest.length === 1 ? '' : 's'}</span>
          </summary>
          <ul className="space-y-2.5 px-3 pb-3">
            {rest.map((candidate, i) => (
              <CandidateRow
                key={candidate.release_group_id || i + 1}
                candidate={candidate}
                index={i + 1}
                selectedRgid={selectedRgid}
                onUseCandidate={onUseCandidate}
              />
            ))}
          </ul>
        </details>
      ) : null}
    </section>
  );
}
