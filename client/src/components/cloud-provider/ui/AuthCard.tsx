"use client";

import React, { ReactNode } from "react";
import Image from "next/image";
import {
  Card,
  CardHeader,
  CardContent,
  CardDescription,
} from "@/components/ui/card";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Alert, AlertTitle, AlertDescription } from "@/components/ui/alert";
import { Loader2 } from "lucide-react";

interface AuthCardProps {
  providerLogo: string;           // e.g. "/aws.ico"
  heading: string;               // e.g. "Connect your AWS Account"
  accordionHelp?: ReactNode;     // provider-specific help accordion
  form: ReactNode;               // live credentials form (new connection)
  savedAccounts: ReactNode;      // list / UI for saved accounts
  error?: string | null;         // optional error message
  connecting?: boolean;          // show full-screen spinner overlay
}

/**
 * One shared skeleton for all cloud-provider auth pages.
 * It owns only the high-level layout (logo, tabs, card) –
 * all provider-specific bits are injected via props.
 */
const AuthCard: React.FC<AuthCardProps> = ({
  providerLogo,
  heading,
  accordionHelp,
  form,
  savedAccounts,
  error,
  connecting = false,
}) => {
  const [tab, setTab] = React.useState<"new" | "saved">("new");

  return (
    <div className="container mx-auto max-w-5xl px-4 py-10">
      {/* Header */}
      <div className="text-center mb-8">
        <Image
          src={providerLogo}
          alt="Provider logo"
          width={56}
          height={56}
          className="mx-auto mb-4"
        />
        <h1 className="text-3xl font-semibold">{heading}</h1>
      </div>

      {/* Card with tabs */}
      <Card>
        <CardHeader className="pb-4 border-b">
          <Tabs value={tab} onValueChange={(v) => setTab(v as "new" | "saved")} defaultValue="new">
            <TabsList className="w-full justify-center bg-transparent p-0 gap-2">
              <TabsTrigger value="new" className="flex-1">
                New connection
              </TabsTrigger>
              <TabsTrigger value="saved" className="flex-1">
                Saved accounts
              </TabsTrigger>
            </TabsList>

            {/* New connection */}
            <TabsContent value="new" className="pt-6">
              {accordionHelp}
              {form}
            </TabsContent>

            {/* Saved accounts */}
            <TabsContent value="saved" className="pt-6">
              {savedAccounts}
            </TabsContent>
          </Tabs>
        </CardHeader>
      </Card>

      {/* Error banner */}
      {error && (
        <Alert variant="destructive" className="mt-6">
          <AlertTitle>Error</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {/* Full-page overlay while connecting */}
      {connecting && (
        <div className="fixed inset-0 bg-background/70 backdrop-blur-sm flex items-center justify-center z-50">
          <div className="flex items-center gap-3 bg-card border p-6 rounded-xl shadow-lg">
            <Loader2 className="w-6 h-6 animate-spin text-primary" />
            <span className="font-medium">Establishing secure connection…</span>
          </div>
        </div>
      )}
    </div>
  );
};

export default AuthCard; 