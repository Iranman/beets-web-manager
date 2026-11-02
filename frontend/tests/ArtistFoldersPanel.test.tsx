import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, expect, it, vi } from 'vitest';
import { ArtistFoldersPanel } from '../src/features/artistfolders/ArtistFoldersPanel';

afterEach(cleanup);

const group = {
  key: 'al',
  match_type: 'name',
  canonical: { name: 'Al Campbell' },
  variants: [{ name: 'Al Campbell', db_albums: 1 }],
  sources: [{ name: 'Al  Campbell', db_tracks: 1 }],
  source_audio_files: 1,
};

vi.mock('../src/lib/hooks', () => ({
  useJobPoll: (id: string | null) => ({ job: id === 'scan' ? { status: 'success' } : null }),
}));
vi.mock('../src/api/client', () => ({
  scanArtistFolders: vi.fn().mockResolvedValue({ job_id: 'scan' }),
  getJobResult: () => ({ groups: [group] }),
  mergeArtistFolders: vi.fn(),
  stampMbidFolders: vi.fn(),
}));

it('keeps Stamp and Merge visible but disabled, with the reason, while Scan still works', async () => {
  render(<MemoryRouter><ArtistFoldersPanel /></MemoryRouter>);
  expect(screen.getByText(/stamping and merging are not available/)).toBeTruthy();
  expect((screen.getByRole('button', { name: 'Stamp IDs' }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole('button', { name: 'Dry Run' }) as HTMLButtonElement).disabled).toBe(true);

  fireEvent.click(screen.getByRole('button', { name: 'Scan' }));
  fireEvent.click(await screen.findByLabelText('Select all'));
  expect((screen.getByRole('button', { name: 'Merge (1)' }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole('button', { name: 'Dry run' }) as HTMLButtonElement).disabled).toBe(true);
});
