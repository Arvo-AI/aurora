/**
 * Validated server-side environment variables.
 * Importing this module will throw at startup if required vars are missing.
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
  BACKEND_URL: required('BACKEND_URL'),
  INTERNAL_API_SECRET: requiredInProduction('INTERNAL_API_SECRET'),
};
