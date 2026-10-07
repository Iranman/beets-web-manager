import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { WantedPanel } from '../src/features/wanted/WantedPanel';
import {
  getLidarrArtistAlbumsByName,
  getWantedLidarr,
  getYtdlpStatus,
  reimportDisk,
  startAlbumDownload,
} from '../src/api/client';

vi.mock('../src/api/client', () => ({
  getLidarrArtistAlbumsByName: vi.fn(),
  getWantedLidarr: vi.fn(),
  getYtdlpStatus: vi.fn(),
  reimportDisk: vi.fn(),
  runLidarrAlbumSearch: vi.fn(),
  startAlbumDownload: vi.fn(),
}));
vi.mock('../src/lib/hooks', () => ({ useJobPoll: () => ({ job: null, error: null }) }));

const RGID = '11111111-2222-3333-4444-555555555555';

describe('WantedPanel release-group identity (#242 F-4/F-6)', () => {
  beforeEach(() => {
    vi.mocked(getWantedLidarr).mockResolvedValue({
      ok: true,
      total: 1,
      missing: [{
        artist: 'Artist', album: 'Wanted Title', year: '2001', type: 'Album', lidarr_id: 7, monitored: true,
        mb_albumid: '', mb_releasegroupid: RGID, mb_url: `https://musicbrainz.org/release-group/${RGID}`,
      }],
    });
    vi.mocked(getYtdlpStatus).mockResolvedValue({ ok: true, ready: false, enabled: false, cookie_file: '', cookie_candidates: [], message: 'off' } as never);
    // Lidarr status row: different id and title, so it can only match by release group.
    vi.mocked(getLidarrArtistAlbumsByName).mockResolvedValue({
      ok: true, found: true, lidarr_artist: 'Artist', artist_path: '/music/Artist',
      albums: [{
        lidarr_id: 99, title: 'Other Name', year: '2001', album_type: 'Album', monitored: true,
        track_file_count: 10, total_track_count: 10, percent: 100, mb_albumid: RGID.toUpperCase(),
        cover_url: '', disk_path: '/music/Artist/Other Name', aldir: '/music/Artist/Other Name',
      }],
    });
    vi.mocked(startAlbumDownload).mockResolvedValue({ ok: true, job_id: 'j1' } as never);
    vi.mocked(reimportDisk).mockResolvedValue({ ok: true, job_id: 'j2' } as never);
  });
  afterEach(() => { vi.resetAllMocks(); cleanup(); });

  it('sends mb_releasegroupid (not a Release id) for downloads and labels it as a release group', async () => {
    render(<WantedPanel />);
    fireEvent.click(await screen.findByRole('button', { name: 'slskd' }));
    await waitFor(() => expect(startAlbumDownload).toHaveBeenCalled());
    const payload = vi.mocked(startAlbumDownload).mock.calls[0][0];
    expect(payload.mb_releasegroupid).toBe(RGID);
    expect(payload.mb_albumid).toBeUndefined();
    expect(screen.getByRole('link', { name: 'MusicBrainz release group' })).toBeTruthy();
    expect(screen.getByTitle('MusicBrainz release group ID').textContent).toBe(RGID);
  });

  it('matches the Lidarr status by release group and imports via a release-group reference', async () => {
    render(<WantedPanel />);
    fireEvent.click((await screen.findAllByRole('button', { name: 'Load Status' }))[0]);
    const importButton = await screen.findByRole('button', { name: 'Import & Tag' });
    await waitFor(() => expect((importButton as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(importButton);
    fireEvent.click(await screen.findByRole('button', { name: 'Confirm' }));
    await waitFor(() => expect(reimportDisk).toHaveBeenCalled());
    expect(vi.mocked(reimportDisk).mock.calls[0][0]).toMatchObject({
      aldir: '/music/Artist/Other Name',
      mb_albumid: `https://musicbrainz.org/release-group/${RGID}`,
    });
  });
});
