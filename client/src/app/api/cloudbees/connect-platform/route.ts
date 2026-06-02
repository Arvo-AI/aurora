import { createCIPostHandler } from "@/lib/ci-api-handler";

export const POST = createCIPostHandler({ slug: "cloudbees", endpoint: "connect-platform", label: "connect CloudBees platform" });
