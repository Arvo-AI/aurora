import { createCIGetHandler } from "@/lib/ci-api-handler";

export const GET = createCIGetHandler({ slug: "jenkins", endpoint: "deployments", label: "deployments" });
