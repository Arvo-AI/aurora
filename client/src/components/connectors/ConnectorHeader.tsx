"use client";

import React from "react";
import { Search, Radar } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";

interface ConnectorHeaderProps {
  searchQuery: string;
  onSearchChange: (query: string) => void;
  selectedCategories: string[];
  onCategoryToggle: (category: string) => void;
  availableCategories: string[];
}

export default function ConnectorHeader({
  searchQuery,
  onSearchChange,
  selectedCategories,
  onCategoryToggle,
  availableCategories,
}: ConnectorHeaderProps) {
  const [discovering, setDiscovering] = React.useState(false);
  const [discoveryMsg, setDiscoveryMsg] = React.useState("");

  const runDiscovery = async () => {
    setDiscovering(true);
    setDiscoveryMsg("");
    try {
      const res = await fetch("/api/prediscovery/run", {
        method: "POST",
        credentials: "include",
      });
      if (res.ok) {
        setDiscoveryMsg("Discovery started");
      } else {
        const data = await res.json().catch(() => ({}));
        setDiscoveryMsg(data.error || "Failed to start");
      }
    } catch {
      setDiscoveryMsg("Failed to start");
    } finally {
      setDiscovering(false);
      setTimeout(() => setDiscoveryMsg(""), 4000);
    }
  };

  return (
    <div className="mb-8 space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-3xl font-bold">Connectors</h1>
        <div className="flex items-center gap-3">
          {discoveryMsg && (
            <span className="text-sm text-muted-foreground">{discoveryMsg}</span>
          )}
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={runDiscovery}
                  disabled={discovering}
                >
                  <Radar className={`h-4 w-4 mr-2 ${discovering ? "animate-spin" : ""}`} />
                  {discovering ? "Discovering..." : "Run Discovery"}
                </Button>
              </TooltipTrigger>
              <TooltipContent side="bottom" className="max-w-xs text-center">
                <p>Scan all connected integrations to map how your services, pipelines, and monitoring are interconnected. Runs automatically once a day and when new connectors are added.</p>
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        </div>
      </div>

      {/* Search Bar */}
      <div className="relative max-w-md">
        <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 h-4 w-4 text-muted-foreground" />
        <Input
          type="text"
          placeholder="Search connectors..."
          value={searchQuery}
          onChange={(e) => onSearchChange(e.target.value)}
          className="pl-10"
        />
      </div>

      {/* Category Filters */}
      {availableCategories.length > 0 && (
        <div className="flex flex-wrap gap-2">
          <span className="text-sm font-medium text-muted-foreground self-center mr-2">
            Filter by:
          </span>
          {availableCategories.map((category) => {
            const isSelected = selectedCategories.includes(category);
            return (
              <Badge
                key={category}
                variant={isSelected ? "default" : "outline"}
                className={`cursor-pointer transition-all hover:scale-105 ${
                  isSelected ? "" : "hover:bg-secondary"
                }`}
                onClick={() => onCategoryToggle(category)}
              >
                {category}
              </Badge>
            );
          })}
        </div>
      )}
    </div>
  );
}
