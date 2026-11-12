import { cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { SleeveArt, albumArtSources } from '../src/components/SleeveArt';

afterEach(cleanup);

const RGID = '11111111-1111-4111-8111-111111111111';

describe('albumArtSources', () => {
  it('uses only the existing art endpoints, in order', () => {
    expect(albumArtSources({ albumId: 7, releaseGroupId: RGID, diskArtPath: '/music/a b/cover.jpg' })).toEqual([
      '/api/albums/7/art',
      `/api/release-art-cache/${RGID}`,
      '/api/disk-art?path=%2Fmusic%2Fa%20b%2Fcover.jpg',
    ]);
  });

  it('skips ids that name no album and malformed release groups', () => {
    expect(albumArtSources({ albumId: 0, releaseGroupId: 'not-an-mbid' })).toEqual([]);
    expect(albumArtSources({})).toEqual([]);
  });
});

describe('SleeveArt', () => {
  it('lazy-loads at a fixed size and falls back to the next source, then the blank sleeve', () => {
    const { container } = render(<SleeveArt size={40} sources={['/a', '/b']} />);
    const img = () => container.querySelector('img');
    expect(img()?.getAttribute('src')).toBe('/a');
    expect(img()?.getAttribute('loading')).toBe('lazy');
    expect(img()?.getAttribute('width')).toBe('40');
    fireEvent.error(img()!);
    expect(img()?.getAttribute('src')).toBe('/b');
    fireEvent.error(img()!);
    expect(img()).toBeNull();
    expect(container.querySelector('svg')).toBeTruthy();
  });

  it('is decorative unless given alt text', () => {
    const { container } = render(<SleeveArt size={40} sources={[]} />);
    expect(container.firstElementChild?.getAttribute('aria-hidden')).toBe('true');
  });
});
