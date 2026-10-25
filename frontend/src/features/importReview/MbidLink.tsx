// Missing-track recovery (ARCH-030) Review UI: truthful MusicBrainz-ID links
// and the carrier-backed recovery identity panel.
//
// Spec (mandatory): every suggested MBID shows the full UUID, is clickable to
// the correct MB entity in a new tab, has a one-click Copy button, is labelled
// by type, shows a Verified/Suggested/Conflicting status, is validated
// (UUID + entity) before a link is built, and an UNVERIFIED id is NEVER
// labelled as a confirmed match.
import { useState } from 'react';
import type { ReviewItem, RecoveryCarrierIdentity, RecoveryExpectedTrack } from '../../api/types';

export type MbEntity = 'release-group' | 'release' | 'recording' | 'artist';
export type MbidStatus = 'verified' | 'suggested' | 'conflicting';

// A MusicBrainz identifier is a UUID. We validate it (and the entity) before
// ever building a musicbrainz.org link, so we never link a malformed id.
export const MBID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const ENTITY_PATH: Record<MbEntity, string> = {
  'release-group': 'release-group',
  release: 'release',
  recording: 'recording',
  artist: 'artist',
};

const ENTITY_LABEL: Record<MbEntity, string> = {
  'release-group': 'Release Group',
  release: 'Release',
  recording: 'Recording',
  artist: 'Artist',
};

export function isValidMbid(id: unknown): id is string {
  return typeof id === 'string' && MBID_RE.test(id.trim());
}

/** The canonical MB entity URL, or '' when the id/entity is invalid. Exported
 * so tests can assert the exact URL a link opens. */
export function mbEntityUrl(entity: MbEntity, id: unknown): string {
  if (!isValidMbid(id) || !(entity in ENTITY_PATH)) return '';
  return `https://musicbrainz.org/${ENTITY_PATH[entity]}/${(id as string).trim().toLowerCase()}`;
}

const STATUS_TEXT: Record<MbidStatus, string> = {
  verified: 'Verified',
  suggested: 'Suggested',
  conflicting: 'Conflicting identity',
};

const STATUS_CLASS: Record<MbidStatus, string> = {
  verified: 'border-emerald-300 bg-emerald-50 text-emerald-800',
  suggested: 'border-amber-300 bg-amber-50 text-amber-900',
  conflicting: 'border-rose-300 bg-rose-50 text-rose-800',
};

async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator?.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall through to the no-op below */
  }
  return false;
}

/**
 * Render one MusicBrainz id truthfully. `status` defaults to `suggested`: a
 * caller must pass `verified` only for an id the backend actually verified, so
 * an unverified suggestion is never shown as a confirmed match.
 */
export function MbidLink({
  id,
  entity,
  status = 'suggested',
  label,
}: {
  id?: string;
  entity: MbEntity;
  status?: MbidStatus;
  label?: string;
}) {
  const [copied, setCopied] = useState(false);
  const trimmed = (id || '').trim();
  const typeLabel = label || ENTITY_LABEL[entity] || 'MusicBrainz ID';
  const url = mbEntityUrl(entity, trimmed);

  const onCopy = async () => {
    const ok = await copyText(trimmed);
    if (ok) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    }
  };

  return (
    <div className="flex flex-wrap items-center gap-2 text-xs" data-mbid-entity={entity}>
      <span className="font-medium text-zinc-600">{typeLabel}:</span>
      {url ? (
        <a
          className="break-all font-mono underline decoration-zinc-400 hover:text-zinc-700"
          href={url}
          target="_blank"
          rel="noreferrer noopener"
          aria-label={`Open ${typeLabel} ${trimmed} on MusicBrainz in a new tab`}
        >
          {trimmed}
        </a>
      ) : (
        <span className="break-all font-mono text-rose-700" title="Not a valid MusicBrainz UUID">
          {trimmed || '—'}
          <span className="ml-1 not-italic">(invalid MusicBrainz ID)</span>
        </span>
      )}
      {url ? (
        <button
          type="button"
          onClick={onCopy}
          aria-label={`Copy ${typeLabel} ID`}
          className="rounded border border-zinc-300 bg-white px-1.5 py-0.5 font-medium text-zinc-600 hover:bg-zinc-50"
        >
          {copied ? 'Copied' : 'Copy ID'}
        </button>
      ) : null}
      <span className={`rounded border px-1.5 py-0.5 font-semibold ${STATUS_CLASS[status]}`}>
        {STATUS_TEXT[status]}
      </span>
    </div>
  );
}

// ───────────────────────── recovery identity ─────────────────────────

export interface RecoveryIdentity {
  carrierId: string;
  existingAlbumId: number;
  mbReleaseGroupId: string;
  releaseId: string;
  destinationPath: string;
  wantedCount: number;
  expectedTracks: RecoveryExpectedTrack[];
}

function fromEvidence(c: RecoveryCarrierIdentity): RecoveryIdentity {
  return {
    carrierId: c.recovery_carrier_id || '',
    existingAlbumId: Number(c.existing_album_id || 0),
    mbReleaseGroupId: c.mb_releasegroupid || '',
    releaseId: c.release_id || '',
    destinationPath: c.destination_album_path || '',
    wantedCount: (c.wanted_tracks || []).length,
    expectedTracks: c.expected_tracks || [],
  };
}

/**
 * Extract the structured recovery identity a carrier-backed Review item
 * carries, or null when the item is an ordinary (new-album) import. Prefers
 * `evidence.recovery_carrier` (the whole packet); falls back to the `recovery_*`
 * fields flattened on the suggestion. Never parsed from a folder name.
 */
export function getRecoveryIdentity(item: ReviewItem): RecoveryIdentity | null {
  const fromEv = item.evidence?.recovery_carrier;
  if (fromEv && (fromEv.recovery || fromEv.mb_releasegroupid)) return fromEvidence(fromEv);
  const sug = item.suggestion;
  if (sug && (sug.recovery || sug.recovery_mb_releasegroupid)) {
    return {
      carrierId: sug.recovery_carrier_id || '',
      existingAlbumId: Number(sug.recovery_existing_album_id || 0),
      mbReleaseGroupId: sug.recovery_mb_releasegroupid || '',
      releaseId: sug.recovery_release_id || '',
      destinationPath: sug.recovery_destination_path || '',
      wantedCount: (sug.recovery_wanted_tracks || []).length,
      expectedTracks: sug.recovery_expected_tracks || [],
    };
  }
  return null;
}

function trackNumbers(rec: RecoveryIdentity): string {
  const nums = rec.expectedTracks
    .map((t) => t.track)
    .filter((n) => n !== null && n !== undefined && String(n).trim() !== '')
    .map((n) => String(n));
  if (nums.length) return nums.length === 1 ? `track ${nums[0]}` : `tracks ${nums.join(', ')}`;
  const n = rec.expectedTracks.length || rec.wantedCount;
  return n > 0 ? `${n} track${n === 1 ? '' : 's'}` : 'missing tracks';
}

function basename(path: string): string {
  return path.split(/[\\/]/).filter(Boolean).pop() || '';
}

/**
 * Carrier-backed recovery items render VISUALLY DISTINCT from a new-album
 * import: the album being recovered INTO is an existing library album, and the
 * MBIDs shown are its identity, not a confirmed match for the staged files.
 * All MBIDs are shown as `suggested` (unverified) because the carrier reached
 * Review precisely because it could not auto-resolve.
 */
export function RecoveryIdentityPanel({ item }: { item: ReviewItem }) {
  const rec = getRecoveryIdentity(item);
  if (!rec) return null;
  const album =
    item.album ||
    item.suggestion?.album ||
    basename(rec.destinationPath) ||
    'existing album';

  return (
    <section
      className="mt-3 rounded border border-l-4 border-violet-300 border-l-violet-500 bg-violet-50 p-3"
      aria-label="Missing-track recovery"
      data-recovery-panel="true"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="rounded border border-violet-400 bg-violet-100 px-2 py-0.5 text-xs font-semibold text-violet-900">
          Missing-track recovery
        </span>
        <span className="text-xs font-medium text-violet-900">Not a new album</span>
      </div>
      <h3 className="mt-2 text-sm font-semibold text-violet-950">
        Recovery of {album} — {trackNumbers(rec)}
      </h3>
      <p className="mt-1 text-xs text-violet-900">
        These IDs identify the existing library album this download is being recovered into. They are
        not a confirmed match for the staged files yet — verify before importing.
      </p>

      <dl className="mt-2 space-y-1.5">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <dt className="font-medium text-zinc-600">Target album:</dt>
          <dd className="text-zinc-800">
            {album}
            {rec.existingAlbumId > 0 ? (
              <span className="ml-1 font-mono text-zinc-500">(in library · album {rec.existingAlbumId})</span>
            ) : null}
          </dd>
        </div>
        <div>
          <MbidLink entity="release-group" id={rec.mbReleaseGroupId} status="suggested" label="Album Release Group" />
        </div>
        {rec.releaseId ? (
          <div>
            <MbidLink entity="release" id={rec.releaseId} status="suggested" label="Release (edition)" />
          </div>
        ) : null}
        {rec.destinationPath ? (
          <div className="flex flex-wrap items-baseline gap-2 text-xs">
            <dt className="font-medium text-zinc-600">Destination:</dt>
            <dd className="break-all font-mono text-zinc-600">{rec.destinationPath}</dd>
          </div>
        ) : null}
      </dl>

      {rec.expectedTracks.length ? (
        <div className="mt-2">
          <div className="text-xs font-semibold text-violet-950">Expected tracks</div>
          <ul className="mt-1 space-y-1.5">
            {rec.expectedTracks.map((t, i) => (
              <li key={t.recording_id || `${t.track ?? ''}:${i}`} className="text-xs">
                <span className="font-medium text-zinc-700">
                  {t.track !== null && t.track !== undefined && String(t.track).trim() !== ''
                    ? `Track ${t.track}`
                    : `Track ${i + 1}`}
                </span>
                {t.title ? <span className="ml-1 text-zinc-600">· {t.title}</span> : null}
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
  );
}
