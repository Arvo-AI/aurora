"use client";

import React from "react";
import { Search } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";

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
  return (
    <div className="mb-8 space-y-6">
      <div>
        <h1 className="text-3xl font-bold mb-3">Connectors</h1>
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
