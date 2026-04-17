import { createCIGetHandler, createCIPutHandler } from "@/lib/ci-api-handler";

export const GET = createCIGetHandler({ slug: "codefresh", endpoint: "rca-settings", label: "Codefresh RCA settings" });
export const PUT = createCIPutHandler({ slug: "codefresh", endpoint: "rca-settings", label: "update Codefresh RCA settings" });
