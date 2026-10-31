import { expect, test as setup } from '@playwright/test';
import { PASSWORD } from '../env';

setup('wrong password is refused, right password signs in', async ({ page }) => {
  await page.goto('/login');
  await page.getByLabel(/user/i).fill('admin');
  await page.getByLabel(/password/i).fill('not-the-password');
  await page.getByRole('button', { name: /sign in|log in|login/i }).click();
  await expect(page).toHaveURL(/\/login/);
  await expect(page.getByText('Sign-In Error:')).toBeVisible();

  await page.getByLabel(/password/i).fill(PASSWORD);
  await page.getByRole('button', { name: /sign in|log in|login/i }).click();
  await expect(page).toHaveURL(/\/library/);
  await expect(page.getByRole('heading', { name: 'Browse music library' })).toBeVisible();
  await page.context().storageState({ path: 'e2e/test-results/.auth.json' });
});
