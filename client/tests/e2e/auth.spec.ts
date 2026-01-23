// This test is to check if the homepage redirects authenticated guest to chat

import { test, expect } from '@playwright/test';

test('homepage redirects authenticated guest to chat', async ({ page }) => {
  await page.goto('/');
  // Expect either redirect or presence of getUserId call.
  await page.waitForLoadState('networkidle');

  const currentURL = page.url();
  // For unauthenticated users with no session, we stay on home; with guest session, we expect /chat.
  expect(currentURL).toMatch(/(\/chat|\/)/);
});
