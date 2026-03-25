"use client";

import React, { useState, useEffect } from "react";
import { Search } from "lucide-react";

interface Props {
  provider: string;
  category: string;
  search: string;
  providers: string[];
  categories: string[];
  onFilterChange: (provider: string, category: string) => void;
  onSearchChange: (search: string) => void;
}

function FilterButton({
  label,
  isActive,
  onClick,
}: {
  label: string;
  isActive: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`px-3 py-1.5 text-xs font-medium rounded-md border transition-colors ${
        isActive
          ? "bg-foreground text-background border-foreground"
          : "bg-card text-muted-foreground border-border hover:bg-muted hover:text-foreground"
      }`}
    >
      {label}
    </button>
  );
}

export default function ResourceFilters({
  provider,
  category,
  search,
  providers,
  categories,
  onFilterChange,
  onSearchChange,
}: Props) {
  const [localSearch, setLocalSearch] = useState(search);

  useEffect(() => {
    setLocalSearch(search);
  }, [search]);

  useEffect(() => {
    const timer = setTimeout(() => {
      if (localSearch !== search) {
        onSearchChange(localSearch);
      }
    }, 400);
    return () => clearTimeout(timer);
  }, [localSearch, search, onSearchChange]);

  return (
    <div className="space-y-3">
      {/* Provider filter */}
      <div className="flex flex-wrap items-center gap-2">
        <FilterButton
          label="All Providers"
          isActive={!provider}
          onClick={() => onFilterChange("", category)}
        />
        {providers.map((p) => (
          <FilterButton
            key={p}
            label={p.toUpperCase()}
            isActive={provider === p}
            onClick={() => onFilterChange(provider === p ? "" : p, category)}
          />
        ))}
      </div>

      {/* Category filter */}
      <div className="flex flex-wrap items-center gap-2">
        <FilterButton
          label="All Types"
          isActive={!category}
          onClick={() => onFilterChange(provider, "")}
        />
        {categories.map((c) => (
          <FilterButton
            key={c}
            label={c}
            isActive={category === c}
            onClick={() => onFilterChange(provider, category === c ? "" : c)}
          />
        ))}
      </div>

      {/* Search */}
      <div className="relative max-w-sm">
        <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
        <input
          type="text"
          value={localSearch}
          onChange={(e) => setLocalSearch(e.target.value)}
          placeholder="Search resources..."
          className="w-full pl-9 pr-4 py-2 text-sm rounded-md border border-border bg-card text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
        />
      </div>
    </div>
  );
}
