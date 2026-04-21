export async function register() {
  if (process.env.NEXT_RUNTIME === 'nodejs') {
    const { env } = await import('@/lib/server-env');
    // Trigger each getter to validate env vars at server startup
    Boolean(env.BACKEND_URL);
    Boolean(env.INTERNAL_API_SECRET);
  }
}
