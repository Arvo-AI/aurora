// Risk Report funnel events: entry_card_clicked -> report_requested (server)
// -> report_completed (server) -> install_cta_clicked.
export function trackRiskReport(event: string, extra: Record<string, string> = {}) {
  void fetch('/api/proxy/risk-report/events', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ event, ...extra }),
    keepalive: true, // survive the navigation the click triggers
  }).catch(() => {});
}
