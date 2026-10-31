import { expect, test } from '../fixtures';

test('System reports the plugin enabled in config.yaml and the ones that are not', async ({ page }) => {
  await page.goto('/system');
  const main = page.getByRole('main');
  // ftintitle from config.yaml, plus the web and webmanager plugins the app always loads.
  await expect(main).toContainText('Enabled plugins: 3');
  await expect(main).toContainText(/zero\W*Installed but disabled/);
  await expect(main).toContainText(/ftintitle\W*Configured/);
});

test('Recheck plugins asks the backend and the report matches the page', async ({ page }) => {
  await page.goto('/system');
  const verified = page.waitForResponse((r) => r.url().includes('/api/plugins/verify'));
  await page.getByRole('button', { name: 'Recheck plugins' }).click();
  const response = await verified;
  expect(response.ok()).toBeTruthy();
  const report = await response.json();
  expect(JSON.stringify(report)).toContain('ftintitle');
  await expect(page.getByRole('main')).toContainText(`${report.required_healthy_count} of ${report.required_count} Healthy`);
});

test('Plugin Controls: runtime install stays blocked and a panel opens to its actions', async ({ page }) => {
  await page.goto('/config');
  const main = page.getByRole('main');
  await expect(main).toContainText('runtime package installation disabled');
  await expect(main.getByRole('button', { name: 'Fetch Missing Art' })).toHaveCount(0);
  const panel = main.getByRole('button', { name: /Album Art \(fetchart\)/ });
  await panel.click();
  await expect(main.getByRole('button', { name: 'Fetch Missing Art' })).toBeVisible();
  await panel.click();
  await expect(main.getByRole('button', { name: 'Fetch Missing Art' })).toHaveCount(0);
});
