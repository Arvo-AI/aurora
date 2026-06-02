export function formatRoleName(role: string | null | undefined): string {
  if (!role) return '';
  return role
    .split('_')
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase())
    .join(' ');
}
