import { createCIGetHandler } from "@/lib/ci-api-handler";

export const GET = createCIGetHandler({ slug: "jenkins", endpoint: "webhook-url", label: "webhook URL" });
