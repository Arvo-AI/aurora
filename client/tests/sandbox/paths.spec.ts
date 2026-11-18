import { test, expect } from '@playwright/test';
import { isPublicSandboxPath } from '../../src/lib/sandbox-paths';

test('only the intended sample page and API paths bypass authentication', () => {
  for (const path of ['/sandbox', '/api/sandbox/cases', '/api/sandbox/investigate', '/api/sandbox/challenge']) {
    expect(isPublicSandboxPath(path)).toBe(true);
  }
  for (const path of ['/sandbox/customer-data', '/api/sandbox/challenge/execute', '/api/sandbox/cases/secret', '/sandbox-other', '/api/sandbox/investigate-extra', '/api/chat', '/admin']) {
    expect(isPublicSandboxPath(path)).toBe(false);
  }
});
