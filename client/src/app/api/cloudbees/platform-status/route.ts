import { createCIGetHandler } from "@/lib/ci-api-handler";

export const GET = createCIGetHandler({ slug: "cloudbees", endpoint: "platform-status", label: "CloudBees platform status" });
