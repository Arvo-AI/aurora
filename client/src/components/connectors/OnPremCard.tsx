"use client";

import React from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Card, CardHeader, CardTitle, CardDescription, CardContent, CardFooter } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { KeyRound, Server, Settings } from "lucide-react";

export default function OnPremCard() {
  const router = useRouter();

  return (
    <Card className="flex flex-col hover:shadow-lg transition-all duration-200 hover:border-primary/50">
      <CardHeader>
        <div className="flex items-start justify-between">
          <div className="flex items-center gap-3">
            <div className="p-2 rounded-lg bg-muted">
              <Server className="h-6 w-6 text-foreground" />
            </div>
            <div>
              <CardTitle className="text-lg">On Prem</CardTitle>
              <Badge variant="outline" className="mt-1 text-xs">
                Infrastructure
              </Badge>
            </div>
          </div>
        </div>
      </CardHeader>
      
      <CardContent className="flex-1">
        <CardDescription className="text-sm leading-relaxed">
          Manage SSH keys and configure virtual machines for on-premises infrastructure access.
        </CardDescription>
      </CardContent>
      
      <CardFooter className="flex gap-2">
        <Button 
          onClick={() => router.push("/settings/ssh-keys")}
          className="w-full bg-white text-black hover:bg-gray-100"
        >
          <KeyRound className="h-4 w-4 mr-2" />
          SSH Keys
        </Button>
        
        <Button
          onClick={() => router.push("/vm-config")}
          className="w-full bg-white text-black hover:bg-gray-100"
        >
          <Settings className="h-4 w-4 mr-2" />
          VM Config
        </Button>
      </CardFooter>
    </Card>
  );
}

