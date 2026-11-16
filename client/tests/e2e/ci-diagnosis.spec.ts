import { test, expect, Page } from '@playwright/test';
import { createServer, Server } from 'node:http';

const diagnosis = {
  status: 'likely_cause',
  summary: 'TypeScript rejected a possibly undefined string.',
  evidence: [{ start_line: 6, end_line: 7, reason: 'The compiler reports TS2322.', quote: "Type 'undefined' is not assignable to type 'string'." }],
  suggested_steps: ['Handle the undefined case at the assignment.'],
  verification: ['Rerun the type check and confirm it succeeds.'],
  missing_information: [],
  analysis_ms: 1200,
};

async function consentAndSubmit(page: Page) {
  await page.getByRole('checkbox').check();
  await page.getByRole('button', { name: 'Analyze build log' }).click();
}

let backend: Server;
let forwarded: Record<string, unknown> = {};
test.beforeAll(async () => {
  backend = createServer((req, res) => {
    let body = '';
    req.on('data', chunk => { body += chunk; });
    req.on('end', () => {
      res.setHeader('Content-Type', 'application/json');
      if (req.url === '/api/auth/login' || req.url === '/api/auth/me') {
        res.end(JSON.stringify({ id: 'ci-test-user', name: 'CI Test', email: 'ci@example.test',
          orgId: 'ci-test-org', orgName: 'CI Tests', role: 'admin', emailVerified: true, mustChangePassword: false }));
      } else if (req.url === '/api/ci-diagnosis') {
        forwarded = { path: req.url, user: req.headers['x-user-id'], org: req.headers['x-org-id'], secret: req.headers['x-internal-secret'], body: JSON.parse(body) };
        res.end(JSON.stringify(diagnosis));
      } else {
        res.writeHead(404);
        res.end('{}');
      }
    });
  });
  await new Promise<void>((resolve, reject) => {
    backend.once('error', reject);
    backend.listen(5089, '127.0.0.1', resolve);
  });
});

test.afterAll(async () => {
  if (backend?.listening) {
    backend.closeAllConnections();
    await new Promise<void>((resolve, reject) => backend.close(error => error ? reject(error) : resolve()));
  }
});

test.beforeEach(async ({ page }) => {
  // Exercise Auth.js session creation; only the backend identity service is fake.
  const csrf = await (await page.request.get('/api/auth/csrf')).json();
  await page.request.post('/api/auth/callback/credentials', {
    form: { csrfToken: csrf.csrfToken, email: 'ci@example.test', password: 'synthetic-test-password', callbackUrl: '/onboarding' },
    maxRedirects: 0,
  });
});

test('onboarding leads to a consent-gated sample analysis with evidence', async ({ page }) => {
  let submitted: { log: string; consent: boolean } | undefined;
  await page.route('**/api/ci-diagnosis', async route => {
    submitted = route.request().postDataJSON();
    await route.fulfill({ json: diagnosis });
  });
  await page.goto('/onboarding');
  await page.getByRole('link', { name: /Try Aurora on a failed build/ }).click();
  await expect(page.getByRole('heading', { name: 'Diagnose a failed build' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Analyze build log' })).toBeDisabled();
  await page.getByRole('button', { name: 'Use a sample log' }).click();
  await expect(page.getByRole('button', { name: 'Analyze build log' })).toBeDisabled();
  await consentAndSubmit(page);
  await expect(page.getByRole('heading', { name: 'Likely cause', exact: true })).toBeFocused();
  await expect(page.getByText('Lines 6–7', { exact: true })).toBeVisible();
  expect(submitted?.log).toContain('TS2322');
  expect(submitted?.consent).toBe(true);
  await page.screenshot({ path: '../tmp/ci-diagnosis-desktop.png', fullPage: true });
  await page.getByLabel('Build log', { exact: true }).fill('A different build');
  await expect(page.getByRole('heading', { name: 'Likely cause', exact: true })).toHaveCount(0);
  await expect(page.getByRole('checkbox')).not.toBeChecked();
});

test('insufficient evidence asks for context and renders log content as text', async ({ page }) => {
  await page.route('**/api/ci-diagnosis', route => route.fulfill({ json: {
    ...diagnosis, status: 'insufficient_evidence', summary: 'The exit code alone does not explain the failure.',
    evidence: [{ start_line: 1, end_line: 1, reason: 'Only an exit status is available.', quote: '<img src=x onerror="alert(1)"> exit 1' }],
    suggested_steps: [], verification: [], missing_information: ['Include the error output above the exit code.'],
  } }));
  await page.goto('/onboarding/diagnose');
  await page.getByLabel('Build log', { exact: true }).fill('exit 1');
  await consentAndSubmit(page);
  await expect(page.getByRole('heading', { name: 'More evidence needed' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Suggested next steps' })).toHaveCount(0);
  await expect(page.locator('article img')).toHaveCount(0);
  await expect(page.locator('article pre')).toContainText('<img src=x');
});

test('provider errors preserve input and permit retry', async ({ page }) => {
  let attempts = 0;
  await page.route('**/api/ci-diagnosis', route => {
    attempts++;
    return route.fulfill(attempts === 1
      ? { status: 503, json: { error: 'AI provider unavailable. Please retry.' } }
      : { json: diagnosis });
  });
  await page.goto('/onboarding/diagnose');
  await page.getByRole('button', { name: 'Use a sample log' }).click();
  const log = await page.getByLabel('Build log', { exact: true }).inputValue();
  await consentAndSubmit(page);
  await expect(page.getByRole('alert').filter({ hasText: 'provider unavailable' })).toBeVisible();
  await expect(page.getByLabel('Build log', { exact: true })).toHaveValue(log);
  await page.getByRole('button', { name: 'Analyze build log' }).click();
  await expect(page.getByRole('heading', { name: 'Likely cause', exact: true })).toBeVisible();
  expect(attempts).toBe(2);
});

test('oversized logs are not submitted', async ({ page }) => {
  let requests = 0;
  await page.route('**/api/ci-diagnosis', route => { requests++; return route.abort(); });
  await page.goto('/onboarding/diagnose');
  await page.getByLabel('Build log', { exact: true }).fill('x'.repeat(20_001));
  await page.getByRole('checkbox').check();
  await expect(page.getByText('Shorten the excerpt around the failure to continue.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Analyze build log' })).toBeDisabled();
  expect(requests).toBe(0);
});

test('mobile layout fits and reaches the result', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route('**/api/ci-diagnosis', route => route.fulfill({ json: diagnosis }));
  await page.goto('/onboarding/diagnose');
  await page.getByRole('button', { name: 'Use a sample log' }).click();
  await consentAndSubmit(page);
  await expect(page.getByRole('heading', { name: 'Likely cause', exact: true })).toBeFocused();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: '../tmp/ci-diagnosis-mobile.png', fullPage: true });
});

test('unauthenticated API requests are rejected by middleware', async ({ context, page }) => {
  await context.clearCookies();
  const response = await page.request.post('/api/ci-diagnosis', { data: { log: 'failure', consent: true } });
  expect(response.status()).toBe(401);
});

test('Next proxy forwards server identity and returns a non-cached diagnosis', async ({ page }) => {
  // Contract test of the real Next route; only Flask's external boundary is faked.
  const response = await page.request.post('/api/ci-diagnosis', { data: { log: 'build failure', consent: true } });
  expect(response.status()).toBe(200);
  expect(response.headers()['cache-control']).toBe('no-store');
  expect(forwarded).toEqual({ path: '/api/ci-diagnosis', user: 'ci-test-user', org: 'ci-test-org', secret: 'aurora-ci-diagnosis-test-internal', body: { log: 'build failure', consent: true } });
});
