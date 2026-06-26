import { createCIGetHandler, createCIPostHandler } from "@/lib/ci-api-handler";

export const GET = createCIGetHandler({ slug: "cloudbees", endpoint: "fleet/controllers", label: "CloudBees controller fleet" });
export const POST = createCIPostHandler({ slug: "cloudbees", endpoint: "fleet/controllers", label: "add controllers to fleet" });
