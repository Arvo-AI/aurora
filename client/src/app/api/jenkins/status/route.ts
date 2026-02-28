import { createCIGetHandler } from "@/lib/ci-api-handler";

export const GET = createCIGetHandler({ slug: "jenkins", endpoint: "status", label: "Jenkins status" });
