export const ROLE_ADMIN = "admin" as const;
export const ROLE_EDITOR = "editor" as const;
export const ROLE_VIEWER = "viewer" as const;

export type UserRole = typeof ROLE_ADMIN | typeof ROLE_EDITOR | typeof ROLE_VIEWER;

export const VALID_ROLES: readonly UserRole[] = [
  ROLE_ADMIN,
  ROLE_EDITOR,
  ROLE_VIEWER,
] as const;

export const ROLE_META: Record<UserRole, { label: string; desc: string; description: string }> = {
  [ROLE_ADMIN]: { label: "Admin", desc: "Full access", description: "Full access" },
  [ROLE_EDITOR]: { label: "Editor", desc: "Can edit", description: "Can edit" },
  [ROLE_VIEWER]: { label: "Viewer", desc: "Read only", description: "Read only" },
};

export function canWrite(role: string | undefined | null): boolean {
  return role === ROLE_ADMIN || role === ROLE_EDITOR;
}

export function isAdmin(role: string | undefined | null): boolean {
  return role === ROLE_ADMIN;
}
