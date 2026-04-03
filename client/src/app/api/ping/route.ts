export const dynamic = 'force-dynamic';

export function GET() {
  return new Response('pong', {
    status: 200,
    headers: { 'Cache-Control': 'no-store' },
  });
}
