import fs from 'node:fs';
import { expect, root, test } from '../fixtures';

test('music format preferences: save, reload, read back, restore', async ({ page }) => {
  await page.goto('/config');
  const opus = page.getByRole('checkbox', { name: 'Opus' });
  await expect(opus).not.toBeChecked();
  const save = page.getByRole('button', { name: 'Save', exact: true });
  await expect(save).toBeDisabled();

  await opus.check();
  await expect(page.getByText(/Current order: .*Opus/)).toBeVisible();
  const saved = page.waitForResponse((r) => r.url().includes('/api/settings/music-format') && r.request().method() === 'POST');
  await save.click();
  expect((await saved).ok()).toBeTruthy();

  await page.reload();
  await expect(opus).toBeChecked();
  const stored = await (await page.request.get('/api/settings/music-format')).json();
  expect(stored.preferences.preferred_formats).toContain('opus');

  await opus.uncheck();
  await save.click();
  await page.reload();
  await expect(opus).not.toBeChecked();
});

test('environment setting: edit in System, saved to the data dir, shown after reload', async ({ page }) => {
  await page.goto('/system');
  const field = page.getByRole('textbox', { name: 'Default: 30', exact: true });
  await field.fill('41');
  const saved = page.waitForResponse((r) => r.request().method() !== 'GET' && r.url().includes('/api/setup'));
  await page.getByRole('button', { name: 'Save environment' }).click();
  expect((await saved).ok()).toBeTruthy();

  await page.reload();
  await expect(page.getByRole('main').getByRole('textbox').and(page.locator('input[value="41"]'))).toBeVisible();
  expect(fs.readFileSync(root('data', '.env'), 'utf8')).toContain('BEETS_OUTBOUND_TIMEOUT_SECONDS=41');
});
