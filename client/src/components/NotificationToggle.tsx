import { Switch } from "@/components/ui/switch";

interface NotificationToggleProps {
  title: string;
  description: string;
  icon: React.ReactNode;
  checked: boolean;
  onChange: (checked: boolean) => void;
  isLoading: boolean;
  disabled?: boolean;
}

export function NotificationToggle({
  title,
  description,
  icon,
  checked,
  onChange,
  isLoading,
  disabled = false,
}: NotificationToggleProps) {
  return (
    <div className={`flex items-center justify-between p-4 border rounded-lg ${disabled ? 'opacity-50' : ''}`}>
      <div className="space-y-1 flex-1">
        <h4 className="font-medium flex items-center gap-2">
          {icon}
          {title}
        </h4>
        <p className="text-sm text-muted-foreground">
          {description}
        </p>
      </div>
      <Switch
        checked={checked}
        onCheckedChange={onChange}
        disabled={isLoading || disabled}
        className="ml-4"
      />
    </div>
  );
}
