import { createCIGetHandler } from "@/lib/ci-api-handler";

export const GET = createCIGetHandler({ slug: "cloudbees", endpoint: "webhook-url", label: "webhook URL" });
