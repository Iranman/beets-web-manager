import { afterEach, describe, expect, it, vi } from 'vitest';
import { planAlbumCleanup } from '../src/api/client';

afterEach(() => vi.unstubAllGlobals());

// #184: the album cleanup UI is row-only. The plan request must never ask
// the server to delete files.
describe('planAlbumCleanup', () => {
  it('posts to the plan route without delete_files', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ ok: true, operation_id: 'op' }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }));
    vi.stubGlobal('fetch', fetchMock);
    await planAlbumCleanup(42);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [path, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(path).toBe('/api/albums/42/cleanup/plan');
    expect(init.method).toBe('POST');
    const body = init.body ? JSON.parse(String(init.body)) : {};
    expect(body).not.toHaveProperty('delete_files');
    expect(body).not.toHaveProperty('confirm_delete_files');
  });
});
