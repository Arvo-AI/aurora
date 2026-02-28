import { createCIPostHandler } from "@/lib/ci-api-handler";

export const POST = createCIPostHandler({ slug: "jenkins", endpoint: "connect", label: "connect Jenkins" });
