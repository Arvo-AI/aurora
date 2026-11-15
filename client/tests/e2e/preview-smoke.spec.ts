// Browser smoke test for a freshly booted stack (scripts/preview/up.sh).
//
// It walks the one journey every other feature sits behind: an anonymous
// visitor is turned away, creates a workspace, and ends up signed in. That
// single path crosses the frontend, the Next.js API proxy, the Flask API,
// Postgres, the RBAC seeding and the Auth.js session.
//
// Assumes SMTP is not configured, which is the default: the backend then marks
// a new account as verified instead of emailing a code.

import { test, expect } from '@playwright/test';

// Unique per attempt so CI retries and re-runs against the same database
// never collide on the email or the organization name.
function uniqueSuffix(): string {
  return `${Date.now().toString(36)}${Math.floor(Math.random() * 1e6).toString(36)}`;
}

test.describe('preview smoke', () => {
  // The first request to each page pays for a cold server-side render, and
  // sign-up makes several round trips, so allow more than the 30s default.
  test.describe.configure({ timeout: 90_000 });

  test('anonymous visitor is sent to sign-in', async ({ page }) => {
    await page.goto('/');

    await expect(page).toHaveURL(/\/sign-in/);
    await expect(page.locator('#signin-email')).toBeVisible();
    await expect(page.locator('#signin-password')).toBeVisible();
  });

  test('anonymous API call is refused', async ({ request }) => {
    const response = await request.get('/api/getUserId');

    expect(response.status()).toBe(401);
  });

  test('a new workspace can sign up and gets a session', async ({ page }) => {
    const suffix = uniqueSuffix();
    const email = `preview-${suffix}@example.com`;
    const password = `Preview-${suffix}-pw`;

    await page.goto('/sign-in?mode=signup');
    await page.locator('#org-name').fill(`Preview ${suffix}`);
    await page.locator('#signup-name').fill('Preview Tester');
    await page.locator('#signup-email').fill(email);
    await page.locator('#signup-password').fill(password);
    await page.locator('#confirm-password').fill(password);
    await page.getByRole('button', { name: 'Create workspace' }).click();

    // A new account lands on onboarding. Landing on the email-verification
    // panel instead would mean SMTP is configured in this environment.
    await expect(page).toHaveURL(/\/onboarding/, { timeout: 30_000 });

    // The session is real: a route that needs an identity now answers.
    const response = await page.request.get('/api/getUserId');
    expect(response.status()).toBe(200);
    const body = await response.json();
    expect(body.userId).toBeTruthy();
  });

  test('wrong password is rejected', async ({ page }) => {
    await page.goto('/sign-in');
    await page.locator('#signin-email').fill(`nobody-${uniqueSuffix()}@example.com`);
    await page.locator('#signin-password').fill('not-the-password');
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();

    await expect(page.getByText('Invalid email or password')).toBeVisible();
    await expect(page).toHaveURL(/\/sign-in/);
  });
});
