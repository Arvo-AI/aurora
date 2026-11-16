import { defineConfig } from '@playwright/test';
import { resolve } from 'node:path';

// Isolated local UI tests: synthetic session, no production account or LLM key.
export default defineConfig({
  testDir: './e2e',
  testMatch: 'ci-diagnosis.spec.ts',
  workers: 1,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: 'list',
  outputDir: '../../tmp/ci-diagnosis-browser',
  use: {
    baseURL: 'http://127.0.0.1:3000',
    browserName: 'chromium',
    channel: process.env.PLAYWRIGHT_CHANNEL || undefined,
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  webServer: {
    cwd: resolve(__dirname, '..'),
    command: 'node node_modules/next/dist/bin/next dev --hostname 127.0.0.1 --port 3000',
    url: 'http://127.0.0.1:3000/sign-in',
    reuseExistingServer: false,
    timeout: 120_000,
    env: {
      AUTH_SECRET: 'aurora-ci-diagnosis-local-test-only',
      AUTH_TRUST_HOST: 'true',
      INTERNAL_API_SECRET: 'aurora-ci-diagnosis-test-internal',
      BACKEND_URL: 'http://127.0.0.1:5089',
      FRONTEND_DEV_RUNTIME: 'node',
      NEXT_TELEMETRY_DISABLED: '1',
    },
  },
});
