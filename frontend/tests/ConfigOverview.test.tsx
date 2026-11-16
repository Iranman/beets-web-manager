import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ConfigOverview } from '../src/features/system/ConfigOverview';
import { getConfigOverview } from '../src/api/client';
import type { ConfigOverviewSetting } from '../src/api/types';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

vi.mock('../src/api/client', () => ({
  getConfigOverview: vi.fn(),
}));

const mockGet = vi.mocked(getConfigOverview);

// A secret plaintext the server must never send; it appears in no row's data,
// so it must never appear in the rendered panel either.
const SECRET_PLAINTEXT = 'supersecrettokenvalue12345';

function row(overrides: Partial<ConfigOverviewSetting> & { name: string }): ConfigOverviewSetting {
  return {
    section: 'General',
    layer: 'app',
    source: 'persisted',
    apply: 'live',
    restart_required: false,
    operator: false,
    secret: false,
    configured: true,
    missing: false,
    value: null,
    status_message: '',
    ...overrides,
  };
}

const SETTINGS: ConfigOverviewSetting[] = [
  row({ name: 'AI_MODEL', layer: 'app', source: 'environment', value: 'gpt-test' }),
  row({ name: 'PLEX_TOKEN', layer: 'secret', source: 'persisted', secret: true, value: '********' }),
  row({ name: 'LIDARR_URL', layer: 'app', source: 'persisted', apply: 'restart', restart_required: true, value: 'http://lidarr:8686' }),
  row({
    name: 'BEETS_OUTBOUND_ALLOWLIST', layer: 'deployment', source: 'not_configured',
    apply: 'deploy', restart_required: true, operator: true, configured: false, missing: true, value: null,
  }),
];

describe('ConfigOverview', () => {
  it('renders source, layer, and restart/operator/secret badges without any plaintext secret', async () => {
    mockGet.mockResolvedValue({ ok: true, settings: SETTINGS });
    render(<ConfigOverview />);

    await screen.findByText('AI_MODEL');

    // Source labels for the different precedence layers.
    expect(screen.getByText('Environment')).toBeTruthy();
    expect(screen.getAllByText('Saved settings').length).toBeGreaterThan(0);

    // Non-secret app value is shown; a restart-required setting is flagged.
    expect(screen.getByText('gpt-test')).toBeTruthy();
    expect(screen.getByText('Restart required')).toBeTruthy();

    // Secret row: masked value + Secret badge, and NEVER the plaintext.
    const secretRow = screen.getByTestId('config-row-PLEX_TOKEN');
    expect(within(secretRow).getByText('********')).toBeTruthy();
    // "Secret" appears both as the badge and as the layer label for this row.
    expect(within(secretRow).getAllByText('Secret').length).toBeGreaterThan(0);
    expect(screen.queryByText(SECRET_PLAINTEXT)).toBeNull();

    // Operator-only, missing deployment key: flagged, highlighted, and no value.
    const opRow = screen.getByTestId('config-row-BEETS_OUTBOUND_ALLOWLIST');
    expect(within(opRow).getByText('Operator only')).toBeTruthy();
    // "Not set" shows as both the source badge and the value status for this row.
    expect(within(opRow).getAllByText('Not set').length).toBeGreaterThan(0);
    expect(opRow.getAttribute('data-missing')).toBe('true');
    expect(within(opRow).getByText('Deployment (Compose)')).toBeTruthy();
  });

  it('shows an error alert when the overview fails to load', async () => {
    mockGet.mockRejectedValue(new Error('boom'));
    render(<ConfigOverview />);
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy());
  });
});
