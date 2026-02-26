import { createCIPostHandler } from "@/lib/ci-api-handler";

export const POST = createCIPostHandler({ slug: "cloudbees", endpoint: "connect", label: "connect CloudBees CI" });
