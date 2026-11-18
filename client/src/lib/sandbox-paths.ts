/** Only these sample-only paths may bypass account/organization setup. */
export function isPublicSandboxPath(pathname: string): boolean {
  return ["/sandbox", "/api/sandbox/cases", "/api/sandbox/investigate", "/api/sandbox/challenge"].includes(pathname);
}
