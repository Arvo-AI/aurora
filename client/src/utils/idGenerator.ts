/**
 * Collision-resistant ID generator using crypto.randomUUID()
 * Replaces Date.now() which can collide under bursty events
 */

/**
 * Generate a unique ID using crypto.randomUUID()
 * Falls back to a high-entropy timestamp-based ID if crypto is unavailable
 */
export function generateUniqueId(): string {
  // Use crypto.randomUUID() if available (modern browsers and Node.js 16+)
  if (typeof crypto !== 'undefined' && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  
  // Fallback for environments without crypto.randomUUID()
  // Use high-precision timestamp + random component
  const timestamp = Date.now();
  const random = Math.random().toString(36).substring(2, 15);
  const randomHex = Math.floor(Math.random() * 0xffffff).toString(16).padStart(6, '0');
  return `${timestamp}-${random}-${randomHex}`;
}

/**
 * Generate a numeric ID that's compatible with existing code expecting numbers
 * Uses high-precision performance.now() + random component to avoid collisions
 */
export function generateNumericId(): number {
  // Use performance.now() for sub-millisecond precision + random component
  const highPrecisionTime = typeof performance !== 'undefined' 
    ? performance.now() 
    : Date.now();
  
  // Combine with random bits to avoid collisions
  const randomComponent = Math.floor(Math.random() * 1e6);
  return Math.floor(highPrecisionTime * 1000 + randomComponent);
}
