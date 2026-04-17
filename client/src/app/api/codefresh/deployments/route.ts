import { createCIGetHandler } from "@/lib/ci-api-handler";

export const GET = createCIGetHandler({ slug: "codefresh", endpoint: "deployments", label: "Codefresh deployments" });
