"use client";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Eye, EyeOff, Loader2, ShieldCheck } from "lucide-react";

interface TokenAuthFormProps {
  baseUrl: string;
  setBaseUrl: (v: string) => void;
  username: string;
  setUsername: (v: string) => void;
  password: string;
  setPassword: (v: string) => void;
  showPassword: boolean;
  setShowPassword: (v: boolean) => void;
  loading: boolean;
  onSubmit: (e: React.FormEvent<HTMLFormElement>) => void;
}

export function TokenAuthForm({
  baseUrl, setBaseUrl, username, setUsername,
  password, setPassword, showPassword, setShowPassword,
  loading, onSubmit,
}: TokenAuthFormProps) {
  const isValid = baseUrl && username && password;

  return (
    <form onSubmit={onSubmit} className="space-y-5">
      <div className="grid gap-1.5">
        <Label htmlFor="spinnaker-url" className="text-sm font-medium">Spinnaker Gate URL</Label>
        <Input
          id="spinnaker-url"
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          placeholder="https://spinnaker-gate.example.com"
          required
          disabled={loading}
          className="h-10"
        />
        <p className="text-xs text-muted-foreground">
          Full URL to your Spinnaker Gate API (e.g. https://gate.spinnaker.example.com)
        </p>
      </div>

      <div className="grid gap-1.5">
        <Label htmlFor="spinnaker-username" className="text-sm font-medium">Username</Label>
        <Input
          id="spinnaker-username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          placeholder="your-username"
          required
          disabled={loading}
          className="h-10"
        />
      </div>

      <div className="grid gap-1.5">
        <Label htmlFor="spinnaker-password" className="text-sm font-medium">Password / API Token</Label>
        <div className="relative">
          <Input
            id="spinnaker-password"
            type={showPassword ? "text" : "password"}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="Enter password or API token"
            required
            disabled={loading}
            className="h-10 pr-10"
          />
          <button
            type="button"
            onClick={() => setShowPassword(!showPassword)}
            className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground transition-colors"
            aria-label={showPassword ? "Hide password" : "Show password"}
          >
            {showPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
          </button>
        </div>
      </div>

      <div className="flex items-start gap-2.5 p-3 rounded-lg bg-muted/50 text-xs">
        <ShieldCheck className="h-4 w-4 text-green-600 dark:text-green-500 shrink-0 mt-0.5" />
        <div className="space-y-1">
          <p className="font-medium">Secure Connection</p>
          <p className="text-muted-foreground">
            Your credentials are encrypted and stored in Vault. Aurora monitors applications and pipelines, and can trigger pipelines (e.g., rollback) with your confirmation.
          </p>
        </div>
      </div>

      <Button type="submit" disabled={loading || !isValid} className="w-full h-10">
        {loading ? (<><Loader2 className="h-4 w-4 mr-2 animate-spin" />Connecting...</>) : "Connect Spinnaker"}
      </Button>
    </form>
  );
}
