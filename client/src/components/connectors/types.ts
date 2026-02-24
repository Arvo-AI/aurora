import { LucideIcon } from "lucide-react";

export interface ConnectorConfig {
  id: string;
  name: string;
  description: string;
  icon?: LucideIcon;
  iconPath?: string;
  iconClassName?: string;
  iconColor?: string;
  iconBgColor?: string;
  category?: string;
  path?: string;
  storageKey?: string;
  onConnect?: (userId: string | null) => void | Promise<void>;
  useCustomConnection?: boolean;
  alertsPath?: string;
  alertsLabel?: string;
  overviewPath?: string;
  overviewLabel?: string;
}
