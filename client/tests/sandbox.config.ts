import { defineConfig } from '@playwright/test';

// Contract tests use no browser, running server, model provider or credentials.
export default defineConfig({ testDir: './sandbox', reporter: 'list', timeout: 5000 });
