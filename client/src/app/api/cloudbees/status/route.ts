import { createCIGetHandler } from "@/lib/ci-api-handler";

export const GET = createCIGetHandler({ slug: "cloudbees", endpoint: "status", label: "CloudBees CI status" });
