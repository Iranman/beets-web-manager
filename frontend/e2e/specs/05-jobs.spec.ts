import { expect, holdCleanAll, releaseCleanAll, startFreshCleanAll, test } from '../fixtures';

test.afterEach(releaseCleanAll);

test('job runs with progress, cancels after confirmation, and stays in history with its log', async ({ page }) => {
  holdCleanAll();
  await page.goto('/jobs');
  const cancelledRows = page.getByRole('button', { name: /^Cancelled Clean All/ });
  // 02a-clean-all left a cancelled run in the history: this run must add its own.
  await expect(cancelledRows.first()).toBeVisible();
  const cancelledBefore = await cancelledRows.count();
  await startFreshCleanAll(page);

  // Progress: 2 of 14 tasks checkpointed before the hold; shown in the page and the nav badge.
  const activity = page.locator('section').filter({ has: page.getByRole('heading', { name: 'Current activity' }) });
  // The panel updates by polling, which can outlast the default timeout on a busy machine.
  await expect(activity).toContainText('2 / 14', { timeout: 30_000 });
  await expect(activity).toContainText('Root Folder Repair', { timeout: 30_000 });
  // Background jobs come and go, so the badge can read more than 1.
  await expect(page.getByRole('banner').getByText(/^\d+ running$/)).toBeVisible();

  await activity.getByRole('button', { name: 'Cancel', exact: true }).click();
  await page.getByRole('dialog', { name: 'Cancel running job?' }).getByRole('button', { name: 'Cancel job' }).click();
  await expect(activity).toContainText('Cancelled', { timeout: 30_000 });

  // Persisted: survives a reload, filterable, log retrievable.
  await page.reload();
  await expect(cancelledRows).toHaveCount(cancelledBefore + 1);
  const row = cancelledRows.first();
  await page.getByRole('button', { name: 'Completed', exact: true }).click();
  await expect(page.getByRole('button', { name: /^Cancelled Clean All/ })).toHaveCount(0);
  await page.getByRole('button', { name: 'Cancelled', exact: true }).click();
  await expect(row).toBeVisible();

  const container = row.locator('xpath=ancestor::*[.//button[normalize-space()="View raw log"]][1]');
  await container.getByRole('button', { name: 'View raw log' }).click();
  await expect(page.getByText('Clean All partially completed: cancelled').first()).toBeVisible();
});

test('history search finds jobs by name and hides the rest', async ({ page }) => {
  await page.goto('/jobs');
  const search = page.getByRole('textbox', { name: 'Search jobs…' });
  await search.fill('Clean All');
  await expect(page.getByRole('button', { name: /^Cancelled Clean All/ }).first()).toBeVisible();
  await expect(page.getByRole('button', { name: /^Completed Library disk reconciliation/ })).toHaveCount(0);
  await search.fill('');
  await expect(page.getByRole('button', { name: /^Completed Library disk reconciliation/ }).first()).toBeVisible();
});
