"use client";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Check, Loader2, ShieldCheck, Upload } from "lucide-react";

interface X509AuthFormProps {
  baseUrl: string;
  setBaseUrl: (v: string) => void;
  certContent: string;
  keyContent: string;
  caContent: string;
  certFileName: string;
  keyFileName: string;
  caFileName: string;
  loading: boolean;
  onSubmit: (e: React.FormEvent<HTMLFormElement>) => void;
  onFileUpload: (
    e: React.ChangeEvent<HTMLInputElement>,
    setContent: (v: string) => void,
    setFileName: (v: string) => void,
  ) => void;
  setCertContent: (v: string) => void;
  setKeyContent: (v: string) => void;
  setCaContent: (v: string) => void;
  setCertFileName: (v: string) => void;
  setKeyFileName: (v: string) => void;
  setCaFileName: (v: string) => void;
}

export function X509AuthForm({
  baseUrl, setBaseUrl,
  certContent, keyContent, caContent,
  certFileName, keyFileName, caFileName,
  loading, onSubmit, onFileUpload,
  setCertContent, setKeyContent, setCaContent,
  setCertFileName, setKeyFileName, setCaFileName,
}: X509AuthFormProps) {
  const isValid = baseUrl && certContent && keyContent;

  return (
    <form onSubmit={onSubmit} className="space-y-5">
      <div className="grid gap-1.5">
        <Label htmlFor="spinnaker-x509-url" className="text-sm font-medium">Spinnaker Gate URL</Label>
        <Input
          id="spinnaker-x509-url"
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          placeholder="https://spinnaker-gate-x509.example.com"
          required
          disabled={loading}
          className="h-10"
        />
        <p className="text-xs text-muted-foreground">
          Full URL to your Spinnaker Gate X.509 endpoint (often a separate port, e.g. :8085)
        </p>
      </div>

      <div className="grid gap-1.5">
        <Label className="text-sm font-medium">Client Certificate (PEM)</Label>
        <div className="flex items-center gap-3">
          <Label
            htmlFor="cert-upload"
            className="flex items-center gap-2 px-4 py-2 rounded-md border cursor-pointer hover:bg-muted/50 transition-colors text-sm"
          >
            <Upload className="h-4 w-4" />
            {certFileName || "Choose certificate file"}
          </Label>
          <input
            id="cert-upload"
            type="file"
            accept=".pem,.crt,.cert"
            className="hidden"
            onChange={(e) => onFileUpload(e, setCertContent, setCertFileName)}
            disabled={loading}
          />
          {certContent && (
            <Badge variant="secondary" className="text-xs">
              <Check className="h-3 w-3 mr-1" /> Loaded
            </Badge>
          )}
        </div>
        <p className="text-xs text-muted-foreground">PEM-encoded client certificate (.pem, .crt)</p>
      </div>

      <div className="grid gap-1.5">
        <Label className="text-sm font-medium">Client Private Key (PEM)</Label>
        <div className="flex items-center gap-3">
          <Label
            htmlFor="key-upload"
            className="flex items-center gap-2 px-4 py-2 rounded-md border cursor-pointer hover:bg-muted/50 transition-colors text-sm"
          >
            <Upload className="h-4 w-4" />
            {keyFileName || "Choose key file"}
          </Label>
          <input
            id="key-upload"
            type="file"
            accept=".pem,.key"
            className="hidden"
            onChange={(e) => onFileUpload(e, setKeyContent, setKeyFileName)}
            disabled={loading}
          />
          {keyContent && (
            <Badge variant="secondary" className="text-xs">
              <Check className="h-3 w-3 mr-1" /> Loaded
            </Badge>
          )}
        </div>
        <p className="text-xs text-muted-foreground">PEM-encoded private key (.pem, .key)</p>
      </div>

      <div className="grid gap-1.5">
        <Label className="text-sm font-medium">
          CA Bundle (PEM) <span className="text-muted-foreground font-normal">- optional</span>
        </Label>
        <div className="flex items-center gap-3">
          <Label
            htmlFor="ca-upload"
            className="flex items-center gap-2 px-4 py-2 rounded-md border cursor-pointer hover:bg-muted/50 transition-colors text-sm"
          >
            <Upload className="h-4 w-4" />
            {caFileName || "Choose CA bundle file"}
          </Label>
          <input
            id="ca-upload"
            type="file"
            accept=".pem,.crt,.cert"
            className="hidden"
            onChange={(e) => onFileUpload(e, setCaContent, setCaFileName)}
            disabled={loading}
          />
          {caContent && (
            <Badge variant="secondary" className="text-xs">
              <Check className="h-3 w-3 mr-1" /> Loaded
            </Badge>
          )}
        </div>
        <p className="text-xs text-muted-foreground">
          CA certificate bundle for verifying the server (only needed for self-signed or private CAs)
        </p>
      </div>

      <div className="flex items-start gap-2.5 p-3 rounded-lg bg-muted/50 text-xs">
        <ShieldCheck className="h-4 w-4 text-green-600 dark:text-green-500 shrink-0 mt-0.5" />
        <div className="space-y-1">
          <p className="font-medium">Secure Connection</p>
          <p className="text-muted-foreground">
            Certificate and key data are encrypted and stored in Vault. Aurora monitors applications and pipelines, and can trigger pipelines (e.g., rollback) with your confirmation.
          </p>
        </div>
      </div>

      <Button type="submit" disabled={loading || !isValid} className="w-full h-10">
        {loading ? (<><Loader2 className="h-4 w-4 mr-2 animate-spin" />Connecting...</>) : "Connect Spinnaker"}
      </Button>
    </form>
  );
}
