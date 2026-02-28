import { createCIGetHandler, createCIPutHandler } from "@/lib/ci-api-handler";

export const GET = createCIGetHandler({ slug: "cloudbees", endpoint: "rca-settings", label: "CloudBees RCA settings" });
export const PUT = createCIPutHandler({ slug: "cloudbees", endpoint: "rca-settings", label: "update CloudBees RCA settings" });
