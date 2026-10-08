const API_BASE = "/api/teams";

async function parseJson(response: Response) {
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || "Request failed");
  }
  return data;
}

export const teamsService = {
  async getStatus() {
    const res = await fetch(API_BASE, { credentials: "include" });
    return parseJson(res);
  },
  async connect() {
    const res = await fetch(API_BASE, { method: "POST", credentials: "include" });
    return parseJson(res);
  },
  async disconnect() {
    const res = await fetch(API_BASE, { method: "DELETE", credentials: "include" });
    return parseJson(res);
  },
  async refreshChannels() {
    const res = await fetch("/api/teams/channels/refresh", {
      method: "POST",
      credentials: "include",
    });
    return parseJson(res);
  },
};
