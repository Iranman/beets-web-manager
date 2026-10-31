import { defineConfig, devices } from '@playwright/test';
import { APP_PORT, NO_FRONTEND_PORT, PYTHON } from './e2e/env';

const server = (port: number, extra = '') => ({
  command: `${PYTHON} e2e/harness/serve.py --port ${port} ${extra}`,
  url: `http://127.0.0.1:${port}/api/health`,
  timeout: 120_000,
  reuseExistingServer: false,
  stdout: 'pipe' as const,
  gracefulShutdown: { signal: 'SIGTERM' as const, timeout: 2_000 },
});

export default defineConfig({
  testDir: './e2e/specs',
  // One app, one library, one job engine: the specs share state, so run in order.
  workers: 1,
  fullyParallel: false,
  retries: 0,
  forbidOnly: !!process.env.CI,
  timeout: 90_000,
  expect: { timeout: 20_000 },
  reporter: [['list'], ['html', { open: 'never', outputFolder: 'e2e/report' }]],
  outputDir: 'e2e/test-results',
  globalTeardown: './e2e/global-teardown.ts',
  use: {
    ...devices['Desktop Chrome'],
    baseURL: `http://127.0.0.1:${APP_PORT}`,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    { name: 'auth', testMatch: /auth\.setup\.ts/ },
    {
      name: 'app',
      dependencies: ['auth'],
      use: { storageState: 'e2e/test-results/.auth.json' },
    },
  ],
  webServer: [server(APP_PORT), server(NO_FRONTEND_PORT, '--no-frontend')],
});
