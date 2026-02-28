import { createCIGetHandler, createCIPutHandler } from "@/lib/ci-api-handler";

export const GET = createCIGetHandler({ slug: "jenkins", endpoint: "rca-settings", label: "Jenkins RCA settings" });
export const PUT = createCIPutHandler({ slug: "jenkins", endpoint: "rca-settings", label: "update Jenkins RCA settings" });
