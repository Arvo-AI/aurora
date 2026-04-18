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

export const env = {
  BACKEND_URL: required('BACKEND_URL'),
  INTERNAL_API_SECRET: process.env.INTERNAL_API_SECRET || '',
};
