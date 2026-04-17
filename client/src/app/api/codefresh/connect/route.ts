import { createCIPostHandler } from "@/lib/ci-api-handler";

export const POST = createCIPostHandler({ slug: "codefresh", endpoint: "connect", label: "connect Codefresh" });
