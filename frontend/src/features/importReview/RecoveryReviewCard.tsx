// Missing-track recovery (ARCH-030 Step 5) Import Review card.
//
// Information architecture: WANTED / DOWNLOADED / WENT WRONG / NEXT, with
// exactly ONE primary action and expandable logs/provenance.
//
// Hard rules this card enforces (reqs 3, 6, 7):
//  - The PRIMARY identity is ALWAYS the carrier's canonical Release Group +
//    expected_tracks (the intended destination album), read straight from
//    evidence.recovery_carrier — never chosen from candidate ranking. An
//    unrelated/title-collision AI candidate can therefore NEVER become the
//    primary action or primary identity; it only ever appears in the clearly
//    separated, secondary "Alternative suggestions" section.
//  - Per-dimension confidence: the five matching_decision dimensions render
//    separately, not as one blob.
//  - Provider chips: AcoustID "no match in database" reads differently from
//    "not configured"; AI shows not-run / rejected and its confidence is NEVER
//    shown when it did not actually contribute.
//
// Presentational + read-only. The actual mutation handler (one primary action)
// is passed in from the page; this card never constructs backend state.
import type {
  AiStatus,
  AiSuggestResponse,
  MatchingDecisionDimension,
  MatchingDecisionDimensions,
  ProviderHealthRow,
  RecoveryCarrierIdentity,
  RecoveryExpectedTrack,
  RecoveryTrackStatus,
  ReviewItem,
  ReviewRecordingCandidate,
} from '../../api/types';
import { MbidLink, RecoveryStageIndicator, getRecoveryIdentity, isValidMbid } from './MbidLink';

// ───────────────────────── provider-status classification ─────────────────────────

export type ChipTone = 'ok' | 'warn' | 'muted' | 'error';

export interface ProviderChip {
  provider: 'AcoustID' | 'AI';
  text: string;
  tone: ChipTone;
  /** A longer explanation (reason), shown as a tooltip. */
  detail?: string;
}

const CHIP_CLASS: Record<ChipTone, string> = {
  ok: 'border-emerald-500/40 bg-emerald-950/40 text-emerald-200',
  warn: 'border-amber-500/40 bg-amber-950/40 text-amber-200',
  muted: 'border-graphite-700 bg-graphite-850 text-muted',
  error: 'border-rose-500/40 bg-rose-950/40 text-rose-200',
};

/** AcoustID chip from the item-specific fingerprint evidence. "no match in the
 * database" (a real empty answer) must read differently from "not configured"
 * (never checked). Sourced from evidence.fingerprint + acoustid_status, which
 * are item-specific — provider_health.acoustid is process-global and cannot
 * say whether THIS lookup matched. */
export function classifyAcoustid(
  fingerprint?: { status?: string; unavailable_reason?: string; acoustid_status?: string; score?: number },
  acoustidStatus?: string,
): ProviderChip {
  const status = (fingerprint?.status || '').trim().toLowerCase();
  const reason = (fingerprint?.unavailable_reason || fingerprint?.acoustid_status || acoustidStatus || '')
    .trim()
    .toLowerCase();
  // "unavailable" means it was not actually checked (see reason); "no_result"
  // is a real empty answer from AcoustID.
  if (status === 'no_result' || (!status && acoustidStatus === '' && fingerprint)) {
    return { provider: 'AcoustID', text: 'No match in database', tone: 'warn' };
  }
  if (status && status !== 'unavailable' && !reason) {
    // A concrete positive result (a recording/score came back).
    return { provider: 'AcoustID', text: 'Matched', tone: 'ok' };
  }
  if (reason.includes('not_configured') || reason.includes('not configured')) {
    return { provider: 'AcoustID', text: 'Not configured', tone: 'muted' };
  }
  if (reason.includes('auth')) {
    return { provider: 'AcoustID', text: 'Authentication failed', tone: 'error', detail: reason };
  }
  if (reason.includes('lookup') || reason.includes('unreachable') || reason.includes('timeout')) {
    return { provider: 'AcoustID', text: 'Lookup failed', tone: 'error', detail: reason };
  }
  if (reason) {
    return { provider: 'AcoustID', text: 'Unavailable', tone: 'muted', detail: reason };
  }
  return { provider: 'AcoustID', text: 'Not checked', tone: 'muted' };
}

/** AI chip. AI confidence is NEVER shown unless `contributed` is true; this
 * returns `showConfidence` so the caller can gate any AI confidence value on
 * it. Prefers the structured ai_status (which the backend derived from
 * provider_health.ai); falls back to provider_health.ai when ai_status is
 * absent so a rejected key still reads as rejected, not healthy. */
export function classifyAi(
  aiStatus?: AiStatus,
  providerHealthAi?: ProviderHealthRow,
): ProviderChip & { showConfidence: boolean } {
  const configured = aiStatus?.configured ?? (providerHealthAi ? (providerHealthAi.calls || 0) > 0 : undefined);
  const contributed = Boolean(aiStatus?.contributed);
  const reason = (aiStatus?.unavailable_reason || providerHealthAi?.last_reason || '').trim();
  const outcome = (aiStatus?.last_outcome ?? providerHealthAi?.last_outcome ?? '').toString().toLowerCase();
  const statusCode = aiStatus?.last_status_code ?? providerHealthAi?.last_status_code ?? null;

  if (contributed) {
    return { provider: 'AI', text: 'Contributed', tone: 'ok', showConfidence: true };
  }
  if (configured === false) {
    return { provider: 'AI', text: 'Not configured', tone: 'muted', showConfidence: false, detail: reason };
  }
  // Configured (or unknown) but did not contribute: distinguish rejected from
  // simply not-run / unreachable.
  if (statusCode === 401 || statusCode === 403 || outcome === 'rejected' || /refus|reject|auth|invalid|unauthor/i.test(reason)) {
    return { provider: 'AI', text: 'Rejected', tone: 'error', showConfidence: false, detail: reason || `HTTP ${statusCode ?? ''}`.trim() };
  }
  if (reason || outcome) {
    return { provider: 'AI', text: 'Not run (unavailable)', tone: 'muted', showConfidence: false, detail: reason || outcome };
  }
  return { provider: 'AI', text: 'Not run', tone: 'muted', showConfidence: false };
}

function Chip({ chip }: { chip: ProviderChip }) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-xs font-semibold ${CHIP_CLASS[chip.tone]}`}
      title={chip.detail || undefined}
      data-provider={chip.provider}
      data-provider-state={chip.text}
    >
      <span className="text-muted">{chip.provider}:</span>
      <span>{chip.text}</span>
    </span>
  );
}

// ───────────────────────── dimension + counts helpers ─────────────────────────

const DIMENSION_ORDER: Array<{ key: keyof MatchingDecisionDimensions; label: string }> = [
  { key: 'identifier_syntax', label: 'Identifier syntax' },
  { key: 'mb_entity_lookup', label: 'MusicBrainz lookup' },
  { key: 'album_identity_verification', label: 'Album identity' },
  { key: 'downloaded_recording_verification', label: 'Downloaded recording' },
  { key: 'import_eligibility', label: 'Import eligibility' },
];

function dimensionTone(dim?: MatchingDecisionDimension): ChipTone {
  if (!dim) return 'muted';
  return dim.ok ? 'ok' : 'warn';
}

export interface RecoveryCounts {
  expected: number;
  imported: number;
  present: number; // imported + verified
  pending: number;
}

/** Expected-vs-present counts, from the carrier's expected_tracks and the
 * per-track status list (never guessed from a folder). */
export function recoveryCounts(carrier: RecoveryCarrierIdentity): RecoveryCounts {
  const tracks = carrier.tracks || [];
  const expected = (carrier.expected_tracks || []).length || tracks.length;
  const imported = tracks.filter((t) => t.status === 'imported').length;
  const verified = tracks.filter((t) => t.status === 'verified').length;
  const present = imported + verified;
  return { expected, imported, present, pending: Math.max(0, expected - present) };
}

const STATUS_CLASS: Record<string, string> = {
  imported: 'border-emerald-500/40 bg-emerald-950/40 text-emerald-200',
  verified: 'border-sky-500/40 bg-sky-950/40 text-sky-200',
  downloaded: 'border-graphite-700 bg-graphite-900 text-graphite-100',
  pending: 'border-graphite-700 bg-graphite-850 text-muted',
};

function trackLabel(t: RecoveryExpectedTrack | RecoveryTrackStatus, index: number): string {
  const pos = 'position' in t ? t.position : (t as RecoveryExpectedTrack).track;
  const disc = t.disc;
  const discPart = disc !== null && disc !== undefined && String(disc).trim() !== '' && String(disc) !== '1'
    ? `Disc ${disc} · `
    : '';
  const num = pos !== null && pos !== undefined && String(pos).trim() !== '' ? `Track ${pos}` : `Track ${index + 1}`;
  return `${discPart}${num}`;
}

// ───────────────────────── alternative (secondary) candidates ─────────────────────────

/** AI / alternative candidates to show ONLY in the secondary section. Pulled
 * from the live ai-suggest response and the stored suggestion; the carrier's
 * own identity is never mixed in here. */
function alternativeCandidates(item: ReviewItem, suggestion?: AiSuggestResponse): ReviewRecordingCandidate[] {
  const live = suggestion?.recording_candidates
    ?? suggestion?.suggestion?.recording_candidates
    ?? suggestion?.suggestions?.recording_candidates;
  const stored = item.suggestion?.recording_candidates;
  const list = (live && live.length ? live : stored) || [];
  const carrierRg = (item.evidence?.recovery_carrier?.mb_releasegroupid || '').toLowerCase();
  // Keep the carrier's own RG out of the "alternatives" list — it is the
  // primary identity, not an alternative.
  return list.filter((c) => (c.mb_releasegroupid || '').toLowerCase() !== carrierRg).slice(0, 5);
}

// ───────────────────────── the card ─────────────────────────

export function RecoveryReviewCard({
  item,
  suggestion,
  providerHealth,
  onPrimaryAction,
  primaryActionLabel,
  primaryActionDisabled,
  busy,
}: {
  item: ReviewItem;
  suggestion?: AiSuggestResponse;
  providerHealth?: Record<string, ProviderHealthRow>;
  onPrimaryAction?: () => void;
  primaryActionLabel?: string;
  primaryActionDisabled?: boolean;
  busy?: boolean;
}) {
  const carrier = item.evidence?.recovery_carrier;
  const identity = getRecoveryIdentity(item);
  // Render only for carrier-backed recovery items.
  if (!carrier || !identity) return null;

  const counts = recoveryCounts(carrier);
  const trackStatuses = carrier.tracks || [];
  const decision = carrier.matching_decision || null;
  const dimensions = decision?.dimensions || {};
  const reviewReason = (carrier.review_reason || '').trim();

  // Provider chips. ai_status lives on the live ai-suggest `suggestions`
  // payload (plural); confidence_source likewise.
  const aiStatus = suggestion?.suggestions?.ai_status ?? suggestion?.suggestion?.ai_status ?? suggestion?.ai_status;
  const fingerprint = suggestion?.evidence?.fingerprint ?? item.evidence?.fingerprint;
  const acoustidChip = classifyAcoustid(fingerprint, suggestion?.acoustid_status);
  const aiChip = classifyAi(aiStatus, providerHealth?.ai);

  // Product direction: "auto when deterministic evidence is strong; ELSE
  // MANUAL". The single primary slot serves both:
  //  - AUTO path (action_allowed === true): an automatic import, allowed only
  //    by the backend's deterministic gate. Unchanged.
  //  - MANUAL path (action_allowed !== true): a clearly-labelled human confirm
  //    that routes through the EXISTING human Review import call — the same
  //    onPrimaryAction the pre-redesign footer Apply/Import button used
  //    (page startApply -> confirm dialog -> runApply, which carries NO
  //    recovery_carrier_id and is therefore human-authorized, still fully
  //    server-side validated; it is never the unattended auto-gated advancer).
  // The manual path never flips the auto-gate and never selects an alternative
  // candidate: the target identity is always the carrier's canonical Release
  // Group shown in WANTED above — an unrelated title-collision candidate can
  // never become the confirmed target.
  const actionAllowed = decision?.action_allowed === true;
  const autoLabel = primaryActionLabel || 'Import recovered track';
  const label = actionAllowed ? autoLabel : 'Review & import manually';
  // action_allowed gates only the AUTOMATIC path. The human path is gated only
  // by the page's real block reasons (busy / applyBlockedReason, carried in
  // primaryActionDisabled) — so a legitimately-in-review recovery can still be
  // completed by a human.
  const disabled = Boolean(primaryActionDisabled) || Boolean(busy);

  const alternatives = alternativeCandidates(item, suggestion);
  const confidence = (carrier.confidence || '').trim();

  const destBasename = (identity.destinationPath.split(/[\\/]/).filter(Boolean).pop() || '').trim();
  const albumName = item.album || item.suggestion?.album || destBasename || 'existing album';

  return (
    <section
      className="mt-3 rounded border border-graphite-700 bg-graphite-900"
      aria-label="Missing-track recovery review"
      data-recovery-review-card="true"
    >
      <header className="flex flex-wrap items-center gap-2 border-b border-graphite-700 bg-graphite-900 px-3 py-2">
        <span className="rounded border border-graphite-700 bg-graphite-800 px-2 py-0.5 text-xs font-semibold text-graphite-100">
          Missing-track recovery
        </span>
        <span className="text-xs font-medium text-graphite-100">Recovery into an existing album — not a new import</span>
        {confidence ? (
          <span
            className={`ml-auto rounded border px-1.5 py-0.5 text-xs font-semibold ${
              confidence === 'high'
                ? CHIP_CLASS.ok
                : confidence === 'medium'
                  ? CHIP_CLASS.warn
                  : CHIP_CLASS.muted
            }`}
            data-recovery-confidence={confidence}
          >
            Confidence: {confidence.replace(/_/g, ' ')}
          </span>
        ) : null}
      </header>

      <div className="space-y-4 p-3">
        <RecoveryStageIndicator item={item} />

        {/* ── WANTED ── primary identity: the intended destination album. */}
        <section data-recovery-section="wanted" data-testid="recovery-primary-identity">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-graphite-100">Wanted</h3>
          <p className="mt-1 text-sm font-semibold text-graphite-50">
            {albumName}
            {identity.existingAlbumId > 0 ? (
              <span className="ml-1 font-mono text-xs font-normal text-muted">(library album {identity.existingAlbumId})</span>
            ) : null}
          </p>
          <p className="mt-0.5 text-xs text-muted">
            {counts.present} of {counts.expected} expected track{counts.expected === 1 ? '' : 's'} present ·{' '}
            <span className="font-semibold text-graphite-100">{counts.pending} still missing</span>
          </p>
          <div className="mt-2">
            <MbidLink entity="release-group" id={identity.mbReleaseGroupId} status="suggested" label="Canonical Release Group" />
          </div>
          {identity.releaseId ? (
            <div className="mt-1">
              <MbidLink entity="release" id={identity.releaseId} status="suggested" label="Release (edition)" />
            </div>
          ) : null}

          {(carrier.expected_tracks || []).length ? (
            <div className="mt-2">
              <div className="text-xs font-semibold text-graphite-200">Missing / expected tracks</div>
              <ul className="mt-1 space-y-1.5" data-testid="recovery-expected-tracks">
                {(carrier.expected_tracks || []).map((t, i) => (
                  <li key={t.recording_id || `${t.track ?? ''}:${i}`} className="text-xs">
                    <span className="font-medium text-graphite-200">{trackLabel(t, i)}</span>
                    {t.title ? <span className="ml-1 text-muted">· {t.title}</span> : null}
                    {t.recording_id ? (
                      <div className="mt-0.5">
                        <MbidLink entity="recording" id={t.recording_id} status="suggested" label="Recording" />
                      </div>
                    ) : null}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </section>

        {/* ── DOWNLOADED ── per-track download/verification status. */}
        {trackStatuses.length ? (
          <section data-recovery-section="downloaded">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-graphite-100">Downloaded</h3>
            <ul className="mt-1 space-y-1" data-testid="recovery-track-status">
              {trackStatuses.map((t, i) => (
                <li key={t.recording_id || `${t.position ?? ''}:${i}`} className="flex flex-wrap items-center gap-2 text-xs">
                  <span className="font-medium text-graphite-200">{trackLabel(t, i)}</span>
                  {t.title ? <span className="text-muted">{t.title}</span> : null}
                  <span
                    className={`rounded border px-1.5 py-0.5 font-semibold ${STATUS_CLASS[t.status || 'pending'] || STATUS_CLASS.pending}`}
                    data-track-status={t.status || 'pending'}
                  >
                    {(t.status || 'pending').replace(/_/g, ' ')}
                  </span>
                </li>
              ))}
            </ul>
          </section>
        ) : null}

        {/* ── WENT WRONG ── per-dimension confidence + provider chips. */}
        <section data-recovery-section="went-wrong">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-graphite-100">What needs checking</h3>
          {reviewReason ? (
            <p className="mt-1 text-xs text-rose-200" data-testid="recovery-review-reason">{reviewReason}</p>
          ) : null}

          <div className="mt-2 flex flex-wrap gap-1.5" data-testid="recovery-provider-chips">
            <Chip chip={acoustidChip} />
            <Chip chip={aiChip} />
          </div>

          {decision ? (
            <ul className="mt-2 space-y-1" data-testid="recovery-dimensions">
              {DIMENSION_ORDER.map(({ key, label: dimLabel }) => {
                const dim = (dimensions as Record<string, MatchingDecisionDimension | undefined>)[key];
                if (!dim) return null;
                const tone = dimensionTone(dim);
                return (
                  <li key={key} className="flex flex-wrap items-baseline gap-2 text-xs" data-dimension={key}>
                    <span
                      className={`inline-flex items-center rounded border px-1.5 py-0.5 font-semibold ${CHIP_CLASS[tone]}`}
                      aria-hidden="true"
                    >
                      {dim.ok ? '✓' : '○'}
                    </span>
                    <span className="font-medium text-graphite-200">{dimLabel}:</span>
                    <span className="text-muted">{dim.status || (dim.ok ? 'ok' : 'review')}</span>
                    {dim.detail ? <span className="text-muted">— {dim.detail}</span> : null}
                  </li>
                );
              })}
            </ul>
          ) : (
            <p className="mt-2 text-xs text-muted">No matching decision has been computed yet.</p>
          )}

          {/* AI confidence may appear ONLY when AI actually contributed. */}
          {aiChip.showConfidence && suggestion?.confidence_source === 'ai' && item.suggestion?.confidence ? (
            <p className="mt-1 text-xs text-muted" data-testid="recovery-ai-confidence">
              AI confidence: {item.suggestion.confidence}
            </p>
          ) : null}
        </section>

        {/* ── NEXT ── exactly one primary action. In the AUTO case it is the
            automatic import; in the MANUAL case the same slot becomes the
            human-review confirm (visibly distinct, never an automatic action). */}
        <section data-recovery-section="next" className="flex flex-col gap-2">
          <h3 className="sr-only">Next</h3>
          <div className="flex flex-wrap items-center gap-3">
            {onPrimaryAction ? (
              <button
                type="button"
                onClick={onPrimaryAction}
                disabled={disabled}
                data-testid="recovery-primary-action"
                data-recovery-action-mode={actionAllowed ? 'auto' : 'manual'}
                className={
                  actionAllowed
                    ? 'rounded bg-red-700 px-3 py-1.5 text-sm font-semibold text-white hover:bg-red-600 disabled:cursor-not-allowed disabled:bg-graphite-700 disabled:text-graphite-300'
                    : 'rounded border border-field-border bg-graphite-900 px-3 py-1.5 text-sm font-semibold text-graphite-50 hover:bg-graphite-800 disabled:cursor-not-allowed disabled:border-graphite-700 disabled:text-muted'
                }
              >
                {label}
              </button>
            ) : null}
            {!actionAllowed ? (
              <span className="rounded border border-graphite-700 bg-graphite-900 px-1.5 py-0.5 text-xs font-semibold text-graphite-100">
                Manual review — not an automatic import
              </span>
            ) : null}
          </div>
          {!actionAllowed ? (
            <p className="text-xs text-muted" data-testid="recovery-manual-note">
              Automatic import is not allowed:{' '}
              {decision?.recommended_action || decision?.explanation || 'verify the download matches this album before importing.'}{' '}
              You can still confirm this import by hand — it imports into{' '}
              <span className="font-semibold text-graphite-100">{albumName}</span> (the canonical Release Group shown above),
              never an alternative candidate, and the backend still validates it.
            </p>
          ) : null}
        </section>

        {/* Clearly separated, SECONDARY alternative suggestions. Never primary. */}
        {alternatives.length ? (
          <section
            data-recovery-section="alternatives"
            data-testid="recovery-alternatives"
            className="rounded border border-dashed border-graphite-700 bg-graphite-850 p-2"
          >
            <h3 className="text-xs font-semibold text-muted">
              Alternative suggestions (secondary — not selected, not the recovery target)
            </h3>
            <p className="mt-0.5 text-xs text-muted">
              These are other candidates the matcher saw. They are shown for reference only and are never imported as
              the recovery target.
            </p>
            <ul className="mt-1.5 space-y-1.5">
              {alternatives.map((c, i) => (
                <li key={c.mb_trackid || c.mb_releasegroupid || i} className="text-xs text-muted">
                  <span className="font-medium text-graphite-200">{c.recording_title || c.title || 'Untitled'}</span>
                  {c.recording_artist || c.artist ? <span> — {c.recording_artist || c.artist}</span> : null}
                  {c.album ? <span className="text-muted"> · {c.album}</span> : null}
                  {isValidMbid(c.mb_releasegroupid) ? (
                    <div className="mt-0.5">
                      <MbidLink entity="release-group" id={c.mb_releasegroupid} status="suggested" label="Alt Release Group" />
                    </div>
                  ) : null}
                </li>
              ))}
            </ul>
          </section>
        ) : null}

        {/* Expandable logs / provenance. */}
        {identity.destinationPath || (carrier.provenance && Object.keys(carrier.provenance).length) ? (
          <details className="rounded border border-graphite-700 bg-graphite-850" data-testid="recovery-provenance">
            <summary className="cursor-pointer px-2 py-1 text-xs font-semibold text-muted">
              Provenance &amp; details
            </summary>
            <div className="space-y-1 px-2 pb-2 text-xs text-muted">
              {identity.destinationPath ? (
                <div className="break-all font-mono">Destination: {identity.destinationPath}</div>
              ) : null}
              {identity.carrierId ? <div className="font-mono">Carrier: {identity.carrierId}</div> : null}
              {carrier.recovery_identity_key ? (
                <div className="font-mono">Identity key: {carrier.recovery_identity_key}</div>
              ) : null}
              {carrier.provenance && Object.keys(carrier.provenance).length ? (
                <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded bg-graphite-900 p-2 font-mono text-[11px] text-graphite-200">
                  {JSON.stringify(carrier.provenance, null, 2)}
                </pre>
              ) : null}
            </div>
          </details>
        ) : null}
      </div>
    </section>
  );
}
