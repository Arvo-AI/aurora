import type { PlanTier, PlanLimits } from "./plans";

export interface Subscription {
  plan_tier: PlanTier;
  status: string;
  stripe_customer_id?: string;
  stripe_subscription_id?: string;
  billing_period_start?: string;
  billing_period_end?: string;
  cancel_at_period_end?: boolean;
  limits: PlanLimits;
}

export interface UsageMetric {
  count: number;
  period_start: string;
  period_end: string;
}

export interface Usage {
  [metric: string]: UsageMetric;
}

export async function getSubscription(): Promise<Subscription> {
  const res = await fetch("/api/billing/subscription");
  if (!res.ok) throw new Error("Failed to fetch subscription");
  return res.json();
}

export async function getUsage(): Promise<Usage> {
  const res = await fetch("/api/billing/usage");
  if (!res.ok) throw new Error("Failed to fetch usage");
  const data = await res.json();
  return data.usage;
}

export async function createCheckoutSession(priceKey: string): Promise<string> {
  const res = await fetch("/api/billing/checkout", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ price_key: priceKey }),
  });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.error || "Failed to create checkout");
  }
  const data = await res.json();
  return data.checkout_url;
}

export async function createPortalSession(): Promise<string> {
  const res = await fetch("/api/billing/portal", { method: "POST" });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.error || "Failed to create portal session");
  }
  const data = await res.json();
  return data.portal_url;
}

export async function cancelSubscription(): Promise<void> {
  const res = await fetch("/api/billing/cancel", { method: "POST" });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.error || "Failed to cancel subscription");
  }
}
