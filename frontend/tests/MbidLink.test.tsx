import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { MbidLink, mbEntityUrl, isValidMbid, getRecoveryIdentity, RecoveryIdentityPanel } from '../src/features/importReview/MbidLink';
import type { ReviewItem } from '../src/api/types';

afterEach(cleanup);

const RG = '11111111-1111-4111-8111-111111111111';
const REL = '22222222-2222-4222-8222-222222222222';
const REC = '33333333-3333-4333-8333-333333333333';
const ART = '44444444-4444-4444-8444-444444444444';

describe('mbEntityUrl — every MBID link opens the correct MB entity', () => {
  it('uses the right path per entity type', () => {
    expect(mbEntityUrl('release-group', RG)).toBe(`https://musicbrainz.org/release-group/${RG}`);
    expect(mbEntityUrl('release', REL)).toBe(`https://musicbrainz.org/release/${REL}`);
    expect(mbEntityUrl('recording', REC)).toBe(`https://musicbrainz.org/recording/${REC}`);
    expect(mbEntityUrl('artist', ART)).toBe(`https://musicbrainz.org/artist/${ART}`);
  });

  it('refuses to build a link for a non-UUID or empty id', () => {
    expect(mbEntityUrl('release-group', 'not-a-uuid')).toBe('');
    expect(mbEntityUrl('release-group', '')).toBe('');
    expect(mbEntityUrl('recording', undefined)).toBe('');
    expect(isValidMbid(RG)).toBe(true);
    expect(isValidMbid('nope')).toBe(false);
  });
});

describe('MbidLink', () => {
  it('renders a clickable new-tab link to the correct URL with the full UUID', () => {
    render(<MbidLink entity="release-group" id={RG} />);
    const link = screen.getByRole('link');
    expect(link.getAttribute('href')).toBe(`https://musicbrainz.org/release-group/${RG}`);
    expect(link.getAttribute('target')).toBe('_blank');
    expect(link.getAttribute('rel') || '').toContain('noreferrer');
    expect(link.textContent).toContain(RG); // full UUID, not truncated
  });

  it('builds recording / release / artist links on the right path', () => {
    const { rerender } = render(<MbidLink entity="recording" id={REC} />);
    expect(screen.getByRole('link').getAttribute('href')).toBe(`https://musicbrainz.org/recording/${REC}`);
    rerender(<MbidLink entity="release" id={REL} />);
    expect(screen.getByRole('link').getAttribute('href')).toBe(`https://musicbrainz.org/release/${REL}`);
    rerender(<MbidLink entity="artist" id={ART} />);
    expect(screen.getByRole('link').getAttribute('href')).toBe(`https://musicbrainz.org/artist/${ART}`);
  });

  it('has a one-click Copy button and a type label', () => {
    render(<MbidLink entity="release-group" id={RG} />);
    expect(screen.getByRole('button', { name: /copy release group id/i })).toBeTruthy();
    expect(screen.getByText('Release Group:')).toBeTruthy();
  });

  it('shows no link and flags an invalid UUID instead of linking it', () => {
    render(<MbidLink entity="release-group" id="garbage-id" />);
    expect(screen.queryByRole('link')).toBeNull();
    expect(screen.getByText(/invalid MusicBrainz ID/i)).toBeTruthy();
    // No Copy button for something we won't link.
    expect(screen.queryByRole('button', { name: /copy/i })).toBeNull();
  });

  it('defaults to Suggested and NEVER labels an unverified suggestion as verified', () => {
    render(<MbidLink entity="release-group" id={RG} />);
    expect(screen.getByText('Suggested')).toBeTruthy();
    expect(screen.queryByText('Verified')).toBeNull();
  });

  it('labels a conflicting identity as conflicting, not verified', () => {
    render(<MbidLink entity="release-group" id={RG} status="conflicting" />);
    expect(screen.getByText(/conflicting identity/i)).toBeTruthy();
    expect(screen.queryByText('Verified')).toBeNull();
  });

  it('labels a verified id as Verified only when explicitly verified', () => {
    render(<MbidLink entity="release-group" id={RG} status="verified" />);
    expect(screen.getByText('Verified')).toBeTruthy();
  });
});

describe('getRecoveryIdentity', () => {
  it('reads the whole evidence.recovery_carrier packet', () => {
    const item = {
      id: 'r1',
      type: 'skipped',
      album: 'Kid A',
      evidence: {
        recovery_carrier: {
          recovery: true,
          recovery_carrier_id: 'c1',
          existing_album_id: 42,
          mb_releasegroupid: RG,
          release_id: REL,
          destination_album_path: '/music/Radiohead/Kid A',
          wanted_tracks: [{}],
          expected_tracks: [{ recording_id: REC, track: 7, title: 'Idioteque' }],
        },
      },
    } as unknown as ReviewItem;
    const rec = getRecoveryIdentity(item);
    expect(rec?.existingAlbumId).toBe(42);
    expect(rec?.mbReleaseGroupId).toBe(RG);
    expect(rec?.releaseId).toBe(REL);
    expect(rec?.expectedTracks[0].recording_id).toBe(REC);
  });

  it('falls back to the flattened recovery_* fields on the suggestion', () => {
    const item = {
      id: 'r2',
      type: 'skipped',
      suggestion: {
        recovery: true,
        recovery_existing_album_id: 9,
        recovery_mb_releasegroupid: RG,
        recovery_expected_tracks: [{ recording_id: REC, track: 3 }],
      },
    } as unknown as ReviewItem;
    const rec = getRecoveryIdentity(item);
    expect(rec?.existingAlbumId).toBe(9);
    expect(rec?.mbReleaseGroupId).toBe(RG);
  });

  it('returns null for an ordinary (non-recovery) import item', () => {
    const item = { id: 'n1', type: 'pending_ai', mb_releasegroupid: RG } as unknown as ReviewItem;
    expect(getRecoveryIdentity(item)).toBeNull();
  });
});

describe('RecoveryIdentityPanel', () => {
  const recoveryItem = {
    id: 'r1',
    type: 'skipped',
    album: 'Kid A',
    evidence: {
      recovery_carrier: {
        recovery: true,
        recovery_carrier_id: 'c1',
        existing_album_id: 42,
        mb_releasegroupid: RG,
        release_id: REL,
        destination_album_path: '/music/Radiohead/Kid A',
        wanted_tracks: [{}],
        expected_tracks: [{ recording_id: REC, track: 7, title: 'Idioteque' }],
      },
    },
  } as unknown as ReviewItem;

  it('renders a distinct recovery header naming the album and track', () => {
    render(<RecoveryIdentityPanel item={recoveryItem} />);
    expect(screen.getByText(/Recovery of Kid A — track 7/i)).toBeTruthy();
    expect(screen.getByText(/Missing-track recovery/i)).toBeTruthy();
    expect(screen.getByText(/in library · album 42/i)).toBeTruthy();
  });

  it('shows the target album Release-Group MBID as Suggested, never Verified', () => {
    render(<RecoveryIdentityPanel item={recoveryItem} />);
    const rgLink = screen.getByRole('link', { name: new RegExp(`Open Album Release Group ${RG}`, 'i') });
    expect(rgLink.getAttribute('href')).toBe(`https://musicbrainz.org/release-group/${RG}`);
    // The whole recovery panel must not present any id as a confirmed match.
    const panel = screen.getByLabelText('Missing-track recovery');
    expect(within(panel).queryByText('Verified')).toBeNull();
  });

  it('links the expected-track Recording ID to the recording entity', () => {
    render(<RecoveryIdentityPanel item={recoveryItem} />);
    const recLink = screen.getByRole('link', { name: new RegExp(`Open Recording ${REC}`, 'i') });
    expect(recLink.getAttribute('href')).toBe(`https://musicbrainz.org/recording/${REC}`);
  });

  it('renders nothing for a non-recovery item', () => {
    const item = { id: 'n1', type: 'pending_ai' } as unknown as ReviewItem;
    const { container } = render(<RecoveryIdentityPanel item={item} />);
    expect(container.querySelector('[data-recovery-panel]')).toBeNull();
  });
});
