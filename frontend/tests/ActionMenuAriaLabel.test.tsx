import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ActionMenu } from '../src/features/playlists/playlistShared';

// Per-row overflow menus must carry a distinguishing accessible name so screen
// readers do not announce N identical "Actions" buttons per page.
describe('ActionMenu accessible name', () => {
  afterEach(cleanup);

  const actions = [{ label: 'Repair', onClick: vi.fn() }];

  it('uses triggerAriaLabel as the accessible name when provided', () => {
    render(<ActionMenu triggerAriaLabel="Actions for Nina Simone – Feeling Good" actions={actions} />);
    const trigger = screen.getByRole('button', { name: /Actions for / });
    expect(trigger.getAttribute('aria-label')).toBe('Actions for Nina Simone – Feeling Good');
    expect(trigger.getAttribute('aria-haspopup')).toBe('menu');
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
  });

  it('falls back to the visible label when no triggerAriaLabel is given', () => {
    render(<ActionMenu actions={actions} />);
    const trigger = screen.getByRole('button', { name: 'Actions' });
    expect(trigger.hasAttribute('aria-label')).toBe(false);
  });
});
