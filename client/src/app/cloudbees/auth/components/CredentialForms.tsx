"use client";

import { Eye, EyeOff, Loader2, Plus, Trash2 } from "lucide-react";
import { useState } from "react";

type ConnectionMode = "oc" | "single" | "fleet" | "pat";

export interface FleetControllerInput {
  name: string;
  url: string;
  username: string;
  token: string;
}

interface CredentialFormsProps {
  mode: ConnectionMode;
  loading: boolean;
  urlError: string;
  // OC fields
  ocUrl: string;
  setOcUrl: (v: string) => void;
  ocUsername: string;
  setOcUsername: (v: string) => void;
  ocApiToken: string;
  setOcApiToken: (v: string) => void;
  rolloutToken: string;
  setRolloutToken: (v: string) => void;
  // Single controller fields
  baseUrl: string;
  setBaseUrl: (v: string) => void;
  username: string;
  setUsername: (v: string) => void;
  apiToken: string;
  setApiToken: (v: string) => void;
  // PAT fields
  platformUrl: string;
  setPlatformUrl: (v: string) => void;
  pat: string;
  setPat: (v: string) => void;
  // Actions
  onOCConnect: (e: React.FormEvent<HTMLFormElement>) => void;
  onSingleConnect: (e: React.FormEvent<HTMLFormElement>) => void;
  onPATConnect: (e: React.FormEvent<HTMLFormElement>) => void;
  onFleetConnect: (controllers: FleetControllerInput[]) => void;
  onBack: () => void;
}

export function CredentialForms({
  mode, loading, urlError,
  ocUrl, setOcUrl, ocUsername, setOcUsername, ocApiToken, setOcApiToken,
  rolloutToken, setRolloutToken,
  baseUrl, setBaseUrl, username, setUsername, apiToken, setApiToken,
  platformUrl, setPlatformUrl, pat, setPat,
  onOCConnect, onSingleConnect, onPATConnect, onFleetConnect, onBack,
}: Readonly<CredentialFormsProps>) {
  const [showOcToken, setShowOcToken] = useState(false);
  const [showRolloutToken, setShowRolloutToken] = useState(false);
  const [showToken, setShowToken] = useState(false);
  const [showPat, setShowPat] = useState(false);

  // Fleet (multiple standalone controllers) state
  const [fleetControllers, setFleetControllers] = useState<FleetControllerInput[]>([]);
  const [fcName, setFcName] = useState("");
  const [fcUrl, setFcUrl] = useState("");
  const [fcUsername, setFcUsername] = useState("");
  const [fcToken, setFcToken] = useState("");
  const [fcError, setFcError] = useState("");
  const [bulkJson, setBulkJson] = useState("");
  const [bulkError, setBulkError] = useState("");

  const isHttpUrl = (u: string) => u.startsWith("http://") || u.startsWith("https://");

  const addFleetController = () => {
    setFcError("");
    const url = fcUrl.trim();
    if (!isHttpUrl(url)) {
      setFcError("URL must start with http:// or https://");
      return;
    }
    if (!fcUsername.trim() || !fcToken) {
      setFcError("Username and token are required");
      return;
    }
    setFleetControllers((prev) => [
      ...prev,
      { name: fcName.trim() || url, url, username: fcUsername.trim(), token: fcToken },
    ]);
    setFcName(""); setFcUrl(""); setFcUsername(""); setFcToken("");
  };

  const removeFleetController = (idx: number) => {
    setFleetControllers((prev) => prev.filter((_, i) => i !== idx));
  };

  // Coerce a JSON value to a trimmed string only when it's a string/number;
  // anything else (objects, arrays) becomes "" rather than "[object Object]".
  const strField = (v: unknown): string =>
    typeof v === "string" || typeof v === "number" ? String(v).trim() : "";

  const parseFleetItem = (item: unknown): FleetControllerInput | null => {
    if (!item || typeof item !== "object") return null;
    const o = item as Record<string, unknown>;
    const url = strField(o.url ?? o.base_url);
    const username = strField(o.username);
    const token = strField(o.token ?? o.api_token);
    // Apply the same http(s):// scheme check as manual entry so bulk import
    // doesn't defer a preventable validation error to connect time.
    if (!isHttpUrl(url) || !username || !token) return null;
    return { name: strField(o.name) || url, url, username, token };
  };

  const applyBulkJson = () => {
    setBulkError("");
    let parsed: unknown;
    try {
      parsed = JSON.parse(bulkJson);
    } catch {
      setBulkError("Invalid JSON. Expected an array of {name, url, username, token}.");
      return;
    }
    if (!Array.isArray(parsed)) {
      setBulkError("JSON must be an array of controller objects.");
      return;
    }
    const valid = parsed.map(parseFleetItem).filter((c): c is FleetControllerInput => c !== null);
    if (valid.length === 0) {
      setBulkError("No valid controllers found. Each needs url, username, and token.");
      return;
    }
    setFleetControllers((prev) => [...prev, ...valid]);
    setBulkJson("");
  };

  const fleetCount = fleetControllers.length;
  let fleetConnectLabel: string;
  if (loading) {
    fleetConnectLabel = "Connecting...";
  } else if (fleetCount === 0) {
    fleetConnectLabel = "Connect controllers";
  } else {
    fleetConnectLabel = `Connect ${fleetCount} controller${fleetCount === 1 ? "" : "s"}`;
  }

  return (
    <div className="animate-step-in">
      {mode === "oc" && (
        <>
          <h1 className="text-[28px] font-bold tracking-tight mb-3">Connect your Operations Center</h1>
          <p className="text-[15px] text-[#777] mb-10">
            Aurora will discover your managed controllers and monitor deployments across all of them.
          </p>

          {urlError && <p className="text-[13px] text-red-500 mb-4">{urlError}</p>}

          <form onSubmit={onOCConnect} className="space-y-6">
            <div>
              <label htmlFor="oc-url" className="block text-[13px] text-[#999] mb-2">Operations Center URL</label>
              <input
                id="oc-url"
                type="text"
                value={ocUrl}
                onChange={(e) => setOcUrl(e.target.value)}
                placeholder="https://cjoc.company.com"
                required
                disabled={loading}
                className="w-full px-4 py-3.5 rounded-xl border border-white/[0.08] bg-white/[0.02] text-[15px] placeholder:text-[#333] focus:outline-none focus:border-white/[0.16] transition-colors disabled:opacity-50"
              />
            </div>
            <div>
              <label htmlFor="oc-username" className="block text-[13px] text-[#999] mb-2">Username</label>
              <input
                id="oc-username"
                type="text"
                value={ocUsername}
                onChange={(e) => setOcUsername(e.target.value)}
                placeholder="your-cloudbees-username"
                required
                disabled={loading}
                className="w-full px-4 py-3.5 rounded-xl border border-white/[0.08] bg-white/[0.02] text-[15px] placeholder:text-[#333] focus:outline-none focus:border-white/[0.16] transition-colors disabled:opacity-50"
              />
            </div>
            <div>
              <label htmlFor="oc-token" className="block text-[13px] text-[#999] mb-2">API Token</label>
              <div className="relative">
                <input
                  id="oc-token"
                  type={showOcToken ? "text" : "password"}
                  value={ocApiToken}
                  onChange={(e) => setOcApiToken(e.target.value)}
                  placeholder="11xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
                  required
                  disabled={loading}
                  className="w-full px-4 py-3.5 pr-11 rounded-xl border border-white/[0.08] bg-white/[0.02] text-[15px] placeholder:text-[#333] focus:outline-none focus:border-white/[0.16] transition-colors disabled:opacity-50"
                />
                <button
                  type="button"
                  onClick={() => setShowOcToken(!showOcToken)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-[#555] hover:text-white transition-colors"
                  aria-label={showOcToken ? "Hide token" : "Show token"}
                >
                  {showOcToken ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </button>
              </div>
            </div>

            <div className="rounded-xl bg-white/[0.02] border border-white/[0.04] p-5">
              <p className="text-[13px] text-[#999] mb-3">Where to find your token:</p>
              <ol className="text-[13px] text-[#777] space-y-1.5 list-decimal list-inside">
                <li>Log in to Operations Center</li>
                <li>Click your username (top-right)</li>
                <li>Go to Configure &rarr; API Token</li>
                <li>Click &quot;Add new Token&quot; and copy it</li>
              </ol>
            </div>

            <details>
              <summary className="text-[13px] text-[#777] cursor-pointer hover:text-[#999] transition-colors">
                Feature Management (optional)
              </summary>
              <div className="mt-4">
                <label htmlFor="rollout-token" className="block text-[13px] text-[#999] mb-2">Feature Management API Token</label>
                <div className="relative">
                  <input
                    id="rollout-token"
                    type={showRolloutToken ? "text" : "password"}
                    value={rolloutToken}
                    onChange={(e) => setRolloutToken(e.target.value)}
                    placeholder="Bearer token from Feature Management"
                    disabled={loading}
                    className="w-full px-4 py-3.5 pr-11 rounded-xl border border-white/[0.08] bg-white/[0.02] text-[15px] placeholder:text-[#333] focus:outline-none focus:border-white/[0.16] transition-colors disabled:opacity-50"
                  />
                  <button
                    type="button"
                    onClick={() => setShowRolloutToken(!showRolloutToken)}
                    className="absolute right-3 top-1/2 -translate-y-1/2 text-[#555] hover:text-white transition-colors"
                    aria-label={showRolloutToken ? "Hide token" : "Show token"}
                  >
                    {showRolloutToken ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                  </button>
                </div>
                <p className="text-[11px] text-[#444] mt-2">Enables feature flag change correlation during incident investigation</p>
              </div>
            </details>

            <div className="flex items-center gap-3 pt-4">
              <button type="button" onClick={onBack} className="text-[15px] text-[#777] hover:text-white transition-colors px-4 py-3">Back</button>
              <button
                type="submit"
                disabled={loading || !ocUrl || !ocUsername || !ocApiToken}
                className="flex-1 py-3.5 rounded-xl bg-white text-black font-medium text-[15px] hover:bg-white/90 transition-colors disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center gap-2"
              >
                {loading && <Loader2 className="h-4 w-4 animate-spin" />}
                {loading ? "Connecting..." : "Connect"}
              </button>
            </div>
          </form>
        </>
      )}

      {mode === "single" && (
        <>
          <h1 className="text-[28px] font-bold tracking-tight mb-3">Connect your controller</h1>
          <p className="text-[15px] text-[#777] mb-10">Enter your CloudBees CI instance details.</p>

          {urlError && <p className="text-[13px] text-red-500 mb-4">{urlError}</p>}

          <form onSubmit={onSingleConnect} className="space-y-6">
            <div>
              <label htmlFor="sc-url" className="block text-[13px] text-[#999] mb-2">Controller URL</label>
              <input id="sc-url" type="text" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://cloudbees.example.com" required disabled={loading} className="w-full px-4 py-3.5 rounded-xl border border-white/[0.08] bg-white/[0.02] text-[15px] placeholder:text-[#333] focus:outline-none focus:border-white/[0.16] transition-colors disabled:opacity-50" />
            </div>
            <div>
              <label htmlFor="sc-username" className="block text-[13px] text-[#999] mb-2">Username</label>
              <input id="sc-username" type="text" value={username} onChange={(e) => setUsername(e.target.value)} placeholder="your-cloudbees-username" required disabled={loading} className="w-full px-4 py-3.5 rounded-xl border border-white/[0.08] bg-white/[0.02] text-[15px] placeholder:text-[#333] focus:outline-none focus:border-white/[0.16] transition-colors disabled:opacity-50" />
            </div>
            <div>
              <label htmlFor="sc-token" className="block text-[13px] text-[#999] mb-2">API Token</label>
              <div className="relative">
                <input id="sc-token" type={showToken ? "text" : "password"} value={apiToken} onChange={(e) => setApiToken(e.target.value)} placeholder="11xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" required disabled={loading} className="w-full px-4 py-3.5 pr-11 rounded-xl border border-white/[0.08] bg-white/[0.02] text-[15px] placeholder:text-[#333] focus:outline-none focus:border-white/[0.16] transition-colors disabled:opacity-50" />
                <button type="button" onClick={() => setShowToken(!showToken)} className="absolute right-3 top-1/2 -translate-y-1/2 text-[#555] hover:text-white transition-colors" aria-label={showToken ? "Hide token" : "Show token"}>
                  {showToken ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </button>
              </div>
              <p className="text-[11px] text-[#444] mt-2">Your profile &rarr; Security &rarr; API Token &rarr; Generate</p>
            </div>

            <div className="flex items-center gap-3 pt-4">
              <button type="button" onClick={onBack} className="text-[15px] text-[#777] hover:text-white transition-colors px-4 py-3">Back</button>
              <button type="submit" disabled={loading || !baseUrl || !username || !apiToken} className="flex-1 py-3.5 rounded-xl bg-white text-black font-medium text-[15px] hover:bg-white/90 transition-colors disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center gap-2">
                {loading && <Loader2 className="h-4 w-4 animate-spin" />}
                {loading ? "Connecting..." : "Connect"}
              </button>
            </div>
          </form>
        </>
      )}

      {mode === "fleet" && (
        <>
          <h1 className="text-[28px] font-bold tracking-tight mb-3">Add your controllers</h1>
          <p className="text-[15px] text-[#777] mb-10">
            Add each standalone controller with its own URL, username, and API token. We&apos;ll
            validate each one as you connect — unreachable controllers are still saved and marked
            offline so the rest of the fleet stays usable.
          </p>

          {fcError && <p className="text-[13px] text-red-500 mb-4">{fcError}</p>}

          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-3">
              <input
                type="text"
                value={fcName}
                onChange={(e) => setFcName(e.target.value)}
                placeholder="Name (optional)"
                disabled={loading}
                className="px-4 py-3 rounded-xl border border-white/[0.08] bg-white/[0.02] text-[14px] placeholder:text-[#333] focus:outline-none focus:border-white/[0.16] transition-colors disabled:opacity-50"
              />
              <input
                type="text"
                value={fcUrl}
                onChange={(e) => setFcUrl(e.target.value)}
                placeholder="https://controller.example.com"
                disabled={loading}
                className="px-4 py-3 rounded-xl border border-white/[0.08] bg-white/[0.02] text-[14px] placeholder:text-[#333] focus:outline-none focus:border-white/[0.16] transition-colors disabled:opacity-50"
              />
              <input
                type="text"
                value={fcUsername}
                onChange={(e) => setFcUsername(e.target.value)}
                placeholder="username"
                disabled={loading}
                className="px-4 py-3 rounded-xl border border-white/[0.08] bg-white/[0.02] text-[14px] placeholder:text-[#333] focus:outline-none focus:border-white/[0.16] transition-colors disabled:opacity-50"
              />
              <input
                type="password"
                value={fcToken}
                onChange={(e) => setFcToken(e.target.value)}
                placeholder="API token"
                disabled={loading}
                className="px-4 py-3 rounded-xl border border-white/[0.08] bg-white/[0.02] text-[14px] placeholder:text-[#333] focus:outline-none focus:border-white/[0.16] transition-colors disabled:opacity-50"
              />
            </div>
            <button
              type="button"
              onClick={addFleetController}
              disabled={loading}
              className="flex items-center gap-2 text-[14px] text-[#999] hover:text-white transition-colors disabled:opacity-50"
            >
              <Plus className="h-4 w-4" /> Add controller
            </button>
          </div>

          {/* Added controllers */}
          {fleetControllers.length > 0 && (
            <div className="mt-6 space-y-2">
              {fleetControllers.map((c, idx) => (
                <div key={`${c.url}-${idx}`} className="flex items-center justify-between py-2.5 px-4 rounded-xl bg-white/[0.02] border border-white/[0.04]">
                  <div className="min-w-0">
                    <p className="text-[14px] truncate">{c.name}</p>
                    <p className="text-[12px] text-[#555] truncate">{c.url} · {c.username}</p>
                  </div>
                  <button
                    type="button"
                    onClick={() => removeFleetController(idx)}
                    disabled={loading}
                    className="text-[#666] hover:text-red-400 transition-colors ml-3 flex-shrink-0"
                    aria-label="Remove controller"
                  >
                    <Trash2 className="h-4 w-4" />
                  </button>
                </div>
              ))}
            </div>
          )}

          {/* Bulk import */}
          <details className="mt-6">
            <summary className="text-[13px] text-[#777] cursor-pointer hover:text-[#999] transition-colors">
              Bulk import (JSON)
            </summary>
            <div className="mt-4">
              <textarea
                value={bulkJson}
                onChange={(e) => setBulkJson(e.target.value)}
                placeholder={'[\n  {"name": "ctrl-a", "url": "https://a.example.com", "username": "user", "token": "11a..."},\n  {"name": "ctrl-b", "url": "https://b.example.com", "username": "user", "token": "11b..."}\n]'}
                rows={6}
                disabled={loading}
                className="w-full px-4 py-3 rounded-xl border border-white/[0.08] bg-white/[0.02] text-[12px] font-mono placeholder:text-[#333] focus:outline-none focus:border-white/[0.16] transition-colors disabled:opacity-50"
              />
              {bulkError && <p className="text-[13px] text-red-500 mt-2">{bulkError}</p>}
              <button
                type="button"
                onClick={applyBulkJson}
                disabled={loading || !bulkJson.trim()}
                className="mt-2 text-[13px] text-[#999] hover:text-white transition-colors disabled:opacity-40"
              >
                Add from JSON
              </button>
            </div>
          </details>

          <div className="flex items-center gap-3 pt-8">
            <button type="button" onClick={onBack} className="text-[15px] text-[#777] hover:text-white transition-colors px-4 py-3">Back</button>
            <button
              type="button"
              onClick={() => onFleetConnect(fleetControllers)}
              disabled={loading || fleetControllers.length === 0}
              className="flex-1 py-3.5 rounded-xl bg-white text-black font-medium text-[15px] hover:bg-white/90 transition-colors disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center gap-2"
            >
              {loading && <Loader2 className="h-4 w-4 animate-spin" />}
              {fleetConnectLabel}
            </button>
          </div>
        </>
      )}

      {mode === "pat" && (
        <>
          <h1 className="text-[28px] font-bold tracking-tight mb-3">Platform authentication</h1>
          <p className="text-[15px] text-[#777] mb-10">Enter your CloudBees platform URL and access token.</p>

          {urlError && <p className="text-[13px] text-red-500 mb-4">{urlError}</p>}

          <form onSubmit={onPATConnect} className="space-y-6">
            <div>
              <label htmlFor="pat-url" className="block text-[13px] text-[#999] mb-2">Platform URL</label>
              <input id="pat-url" type="text" value={platformUrl} onChange={(e) => setPlatformUrl(e.target.value)} placeholder="https://your-org.cloudbees.io" required disabled={loading} className="w-full px-4 py-3.5 rounded-xl border border-white/[0.08] bg-white/[0.02] text-[15px] placeholder:text-[#333] focus:outline-none focus:border-white/[0.16] transition-colors disabled:opacity-50" />
            </div>
            <div>
              <label htmlFor="pat-token" className="block text-[13px] text-[#999] mb-2">Personal Access Token</label>
              <div className="relative">
                <input id="pat-token" type={showPat ? "text" : "password"} value={pat} onChange={(e) => setPat(e.target.value)} placeholder="cbp_xxxxxxxxxxxxxxxxxxxxxxxxxxxx" required disabled={loading} className="w-full px-4 py-3.5 pr-11 rounded-xl border border-white/[0.08] bg-white/[0.02] text-[15px] placeholder:text-[#333] focus:outline-none focus:border-white/[0.16] transition-colors disabled:opacity-50" />
                <button type="button" onClick={() => setShowPat(!showPat)} className="absolute right-3 top-1/2 -translate-y-1/2 text-[#555] hover:text-white transition-colors" aria-label={showPat ? "Hide token" : "Show token"}>
                  {showPat ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </button>
              </div>
              <p className="text-[11px] text-[#444] mt-2">Profile &rarr; Personal access tokens &rarr; Create</p>
            </div>

            <div className="flex items-center gap-3 pt-4">
              <button type="button" onClick={onBack} className="text-[15px] text-[#777] hover:text-white transition-colors px-4 py-3">Back</button>
              <button type="submit" disabled={loading || !platformUrl || !pat} className="flex-1 py-3.5 rounded-xl bg-white text-black font-medium text-[15px] hover:bg-white/90 transition-colors disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center gap-2">
                {loading && <Loader2 className="h-4 w-4 animate-spin" />}
                {loading ? "Connecting..." : "Connect"}
              </button>
            </div>
          </form>
        </>
      )}
    </div>
  );
}
