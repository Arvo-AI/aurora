import { createCIGetHandler } from "@/lib/ci-api-handler";

export const GET = createCIGetHandler({ slug: "codefresh", endpoint: "webhook-url", label: "Codefresh webhook URL" });
