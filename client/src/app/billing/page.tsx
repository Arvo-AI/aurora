"use client";

import { useState, useEffect, useCallback } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Check, CreditCard, ExternalLink, Loader2 } from "lucide-react";
import { PLANS, type PlanTier } from "@/lib/billing/plans";
import {
  getSubscription,
  getUsage,
  createCheckoutSession,
  createPortalSession,
  type Subscription,
  type Usage,
} from "@/lib/billing/client";

export default function BillingPage() {
  const [subscription, setSubscription] = useState<Subscription | null>(null);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [loading, setLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [billingInterval, setBillingInterval] = useState<"monthly" | "yearly">("monthly");

  useEffect(() => {
    Promise.all([getSubscription(), getUsage()])
      .then(([sub, usg]) => {
        setSubscription(sub);
        setUsage(usg);
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  const handleUpgrade = useCallback(async (tier: PlanTier) => {
    const priceKey = `${tier}_${billingInterval}`;
    setActionLoading(priceKey);
    try {
      const url = await createCheckoutSession(priceKey);
      window.location.href = url;
    } catch (err) {
      console.error("Checkout failed:", err);
    } finally {
      setActionLoading(null);
    }
  }, [billingInterval]);

  const handleManageBilling = useCallback(async () => {
    setActionLoading("portal");
    try {
      const url = await createPortalSession();
      window.location.href = url;
    } catch (err) {
      console.error("Portal failed:", err);
    } finally {
      setActionLoading(null);
    }
  }, []);

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[60vh]">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    );
  }

  const currentTier = subscription?.plan_tier || "free";
  const isActive = subscription?.status === "active" || subscription?.status === "trialing";

  return (
    <div className="container max-w-6xl mx-auto py-8 space-y-8">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Billing & Plans</h1>
          <p className="text-muted-foreground">Manage your subscription and usage</p>
        </div>
        {currentTier !== "free" && (
          <Button variant="outline" onClick={handleManageBilling} disabled={actionLoading === "portal"}>
            {actionLoading === "portal" ? (
              <Loader2 className="h-4 w-4 mr-2 animate-spin" />
            ) : (
              <CreditCard className="h-4 w-4 mr-2" />
            )}
            Manage Billing
          </Button>
        )}
      </div>

      {/* Current plan summary */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <div>
              <CardTitle>Current Plan</CardTitle>
              <CardDescription>
                {subscription?.billing_period_end
                  ? `Renews ${new Date(subscription.billing_period_end).toLocaleDateString()}`
                  : "Free tier — no billing"}
              </CardDescription>
            </div>
            <Badge variant={isActive ? "default" : "destructive"} className="text-sm">
              {PLANS.find(p => p.tier === currentTier)?.name || "Free"}
            </Badge>
          </div>
        </CardHeader>
        {subscription?.cancel_at_period_end && (
          <CardContent>
            <p className="text-sm text-amber-600">
              Your subscription will cancel at the end of the current billing period.
            </p>
          </CardContent>
        )}
      </Card>

      {/* Usage */}
      {usage && Object.keys(usage).length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Usage This Period</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {Object.entries(usage).map(([metric, data]) => {
              const limit = subscription?.limits?.[metric as keyof typeof subscription.limits];
              const isUnlimited = limit === -1;
              const percentage = isUnlimited ? 0 : limit ? (data.count / (limit as number)) * 100 : 0;

              return (
                <div key={metric} className="space-y-2">
                  <div className="flex justify-between text-sm">
                    <span className="capitalize">{metric.replace(/_/g, " ")}</span>
                    <span className="text-muted-foreground">
                      {data.count} / {isUnlimited ? "Unlimited" : limit}
                    </span>
                  </div>
                  {!isUnlimited && <Progress value={Math.min(percentage, 100)} />}
                </div>
              );
            })}
          </CardContent>
        </Card>
      )}

      {/* Plan selection */}
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-xl font-semibold">Available Plans</h2>
          <Tabs value={billingInterval} onValueChange={(v) => setBillingInterval(v as "monthly" | "yearly")}>
            <TabsList>
              <TabsTrigger value="monthly">Monthly</TabsTrigger>
              <TabsTrigger value="yearly">Yearly (save 20%)</TabsTrigger>
            </TabsList>
          </Tabs>
        </div>

        <div className="grid md:grid-cols-3 gap-6">
          {PLANS.map((plan) => {
            const isCurrent = plan.tier === currentTier;
            const price = billingInterval === "monthly" ? plan.price_monthly : plan.price_yearly;
            const priceKey = `${plan.tier}_${billingInterval}`;

            return (
              <Card
                key={plan.tier}
                className={plan.highlighted ? "border-primary shadow-md" : ""}
              >
                <CardHeader>
                  <div className="flex items-center justify-between">
                    <CardTitle>{plan.name}</CardTitle>
                    {isCurrent && <Badge variant="secondary">Current</Badge>}
                  </div>
                  <CardDescription>{plan.description}</CardDescription>
                  <div className="pt-2">
                    <span className="text-3xl font-bold">${price}</span>
                    {price > 0 && <span className="text-muted-foreground">/mo</span>}
                  </div>
                </CardHeader>
                <CardContent className="space-y-4">
                  <ul className="space-y-2">
                    {plan.features.map((feature) => (
                      <li key={feature} className="flex items-start gap-2 text-sm">
                        <Check className="h-4 w-4 text-green-500 shrink-0 mt-0.5" />
                        {feature}
                      </li>
                    ))}
                  </ul>
                  {!isCurrent && plan.tier !== "free" && (
                    <Button
                      className="w-full"
                      variant={plan.highlighted ? "default" : "outline"}
                      onClick={() => handleUpgrade(plan.tier)}
                      disabled={!!actionLoading}
                    >
                      {actionLoading === priceKey ? (
                        <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                      ) : null}
                      {currentTier === "free" ? "Get Started" : "Upgrade"}
                    </Button>
                  )}
                  {isCurrent && plan.tier !== "free" && (
                    <Button variant="outline" className="w-full" onClick={handleManageBilling}>
                      Manage Plan
                    </Button>
                  )}
                </CardContent>
              </Card>
            );
          })}
        </div>
      </div>
    </div>
  );
}
