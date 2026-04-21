export async function register() {
  if (process.env.NEXT_RUNTIME === 'nodejs') {
    const { env } = await import('@/lib/server-env');
    // Access each getter to trigger validation at server startup
    env.BACKEND_URL;
    env.INTERNAL_API_SECRET;
  }
}
