import { defineConfig, devices } from '@playwright/test';

// Environment detection for better readability
const isRunningInGithubActions = !!process.env.GITHUB_ACTIONS;
const isCI = !!process.env.CI || isRunningInGithubActions;

export default defineConfig({
  testDir: './e2e',
  retries: isCI ? 2 : 0, // Retry flaky tests in CI, fail fast locally
  timeout: 30 * 1000,
  expect: {
    timeout: 5000,
  },
  
  // Configure multiple projects for cross-browser testing
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
    {
      name: 'firefox',
      use: { ...devices['Desktop Firefox'] },
    },
    {
      name: 'webkit',
      use: { ...devices['Desktop Safari'] },
    },
    {
      name: 'edge',
      use: { 
        ...devices['Desktop Edge'],
        channel: 'msedge',
      },
    },
  ],

  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL,
    headless: true, // Always headless by default (use --headed flag to override)
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    // Enable action timeout for better debugging
    actionTimeout: 10000,
    navigationTimeout: 30000,
  },

  // Enhanced reporters for better debugging and visibility
  reporter: [
    ['html', {
      open: isCI ? 'never' : 'on-failure', // Auto-open locally on failure, never in CI to prevent hanging
      outputFolder: 'playwright-report',
      attachmentsBaseURL: isCI ? '/test-results/' : undefined,
    }],
    ['json', { outputFile: 'test-results/results.json' }],
    ['junit', { outputFile: 'test-results/results.xml' }],
    // List reporter for console output
    ['list'],
    // GitHub Actions reporter only in CI
    ...(isRunningInGithubActions ? [['github'] as const] : []),
  ],

  // Output directories for artifacts
  outputDir: 'test-results/',
  
  // Global setup and teardown
  globalSetup: undefined,
  globalTeardown: undefined,

  webServer: {
    command: 'npm run dev',
    port: 3000,
    reuseExistingServer: true,
    timeout: 120 * 1000, // 2 minutes for server startup
  },
});
