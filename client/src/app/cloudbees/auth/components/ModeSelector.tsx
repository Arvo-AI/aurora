"use client";

import { ChevronRight } from "lucide-react";

type ConnectionMode = "oc" | "single" | "pat";

interface ModeSelectorProps {
  onSelect: (mode: ConnectionMode) => void;
}

export function ModeSelector({ onSelect }: Readonly<ModeSelectorProps>) {
  return (
    <div className="animate-step-in">
      <h1 className="text-[28px] font-bold tracking-tight mb-3">How should Aurora connect?</h1>
      <p className="text-[15px] text-[#777] mb-10">
        Choose based on your CloudBees setup. You can always change this later.
      </p>

      <div className="space-y-3">
        <button
          type="button"
          onClick={() => onSelect("oc")}
          className="group w-full p-6 rounded-2xl border border-white/[0.06] hover:border-white/[0.12] bg-white/[0.01] hover:bg-white/[0.02] transition-all text-left flex items-center justify-between"
        >
          <div>
            <p className="text-[15px] font-medium mb-1">Operations Center</p>
            <p className="text-[13px] text-[#777] leading-relaxed">
              Recommended for teams with multiple Jenkins controllers. Aurora discovers all controllers and can investigate across them.
            </p>
          </div>
          <ChevronRight className="h-4 w-4 text-[#555] flex-shrink-0 ml-4 group-hover:translate-x-1 transition-transform" />
        </button>

        <button
          type="button"
          onClick={() => onSelect("single")}
          className="group w-full p-6 rounded-2xl border border-white/[0.06] hover:border-white/[0.12] bg-white/[0.01] hover:bg-white/[0.02] transition-all text-left flex items-center justify-between"
        >
          <div>
            <p className="text-[15px] font-medium mb-1">Single Controller</p>
            <p className="text-[13px] text-[#777] leading-relaxed">
              Connect directly to one CloudBees CI or Jenkins instance.
            </p>
          </div>
          <ChevronRight className="h-4 w-4 text-[#555] flex-shrink-0 ml-4 group-hover:translate-x-1 transition-transform" />
        </button>

        <button
          type="button"
          onClick={() => onSelect("pat")}
          className="group w-full p-6 rounded-2xl border border-white/[0.06] hover:border-white/[0.12] bg-white/[0.01] hover:bg-white/[0.02] transition-all text-left flex items-center justify-between"
        >
          <div>
            <p className="text-[15px] font-medium mb-1">Personal Access Token</p>
            <p className="text-[13px] text-[#777] leading-relaxed">
              Use a platform-level PAT for authentication.
            </p>
          </div>
          <ChevronRight className="h-4 w-4 text-[#555] flex-shrink-0 ml-4 group-hover:translate-x-1 transition-transform" />
        </button>
      </div>

      <p className="text-[13px] text-[#555] mt-8">
        Not sure? Most teams with CloudBees CI use Operations Center.
      </p>
    </div>
  );
}
