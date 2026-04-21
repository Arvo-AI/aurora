/**
 * Validated server-side environment variables.
 * Values are resolved lazily so module-level imports don't throw during
 * Next.js build (where runtime env vars aren't available).
 */

function required(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`Missing required environment variable: ${name}`);
  }
  return value;
}

function requiredInProduction(name: string): string {
  const value = process.env[name];
  const auroraEnv = process.env.AURORA_ENV || 'production';
  if (!value && auroraEnv !== 'dev') {
    throw new Error(
      `FATAL: ${name} is not set and AURORA_ENV="${auroraEnv}" (non-dev). ` +
      `Refusing to start without authentication secrets in production.`
    );
  }
  return value || '';
}

export const env = {
  get BACKEND_URL() { return required('BACKEND_URL'); },
  get INTERNAL_API_SECRET() { return requiredInProduction('INTERNAL_API_SECRET'); },
};
