import { createCIPostHandler } from "@/lib/ci-api-handler";

export const POST = createCIPostHandler({ slug: "cloudbees", endpoint: "disconnect-platform", label: "disconnect CloudBees platform" });
