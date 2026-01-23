/*
 * Browser-side helper that returns a stable device identifier.
 *
 * – First call generates a new crypto-random UUID and stores it in
 *   `localStorage['auroraFingerprint']`.
 * – Subsequent calls return the stored value.
 *
 * The value is **never** sent automatically – callers must include it in
 * e.g. `fetch` headers:
 *   headers: { 'X-Device-Fingerprint': getFingerprint() }
 */
export function getFingerprint(): string {
  if (typeof window === 'undefined') return '';

  const STORAGE_KEY = 'auroraFingerprint';
  let fp = localStorage.getItem(STORAGE_KEY);
  if (!fp) {
    // `crypto.randomUUID` is available in all modern browsers
    fp = crypto.randomUUID();
    localStorage.setItem(STORAGE_KEY, fp);
  }
  return fp;
}
