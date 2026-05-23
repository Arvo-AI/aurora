export type PlanTier = "free" | "pro" | "enterprise";

export interface PlanLimits {
  max_providers: number;
  max_incidents_per_month: number;
  max_team_members: number;
  rca_depth: number;
  max_actions_per_month: number;
}

export interface PlanDefinition {
  name: string;
  tier: PlanTier;
  description: string;
  price_monthly: number;
  price_yearly: number;
  limits: PlanLimits;
  features: string[];
  highlighted?: boolean;
}

export const PLANS: PlanDefinition[] = [
  {
    name: "Free",
    tier: "free",
    description: "For individuals exploring Aurora",
    price_monthly: 0,
    price_yearly: 0,
    limits: {
      max_providers: 2,
      max_incidents_per_month: 20,
      max_team_members: 2,
      rca_depth: 1,
      max_actions_per_month: 10,
    },
    features: [
      "2 cloud provider connections",
      "20 incidents/month",
      "Basic RCA analysis",
      "2 team members",
      "Community support",
    ],
  },
  {
    name: "Pro",
    tier: "pro",
    description: "For growing teams",
    price_monthly: 49,
    price_yearly: 39,
    limits: {
      max_providers: 10,
      max_incidents_per_month: 500,
      max_team_members: 10,
      rca_depth: 3,
      max_actions_per_month: 200,
    },
    features: [
      "10 cloud provider connections",
      "500 incidents/month",
      "Deep RCA with correlation",
      "10 team members",
      "Email notifications",
      "SSO authentication",
      "Custom actions",
      "Priority support",
    ],
    highlighted: true,
  },
  {
    name: "Enterprise",
    tier: "enterprise",
    description: "For organizations at scale",
    price_monthly: 199,
    price_yearly: 159,
    limits: {
      max_providers: -1,
      max_incidents_per_month: -1,
      max_team_members: -1,
      rca_depth: -1,
      max_actions_per_month: -1,
    },
    features: [
      "Unlimited provider connections",
      "Unlimited incidents",
      "Advanced RCA with full depth",
      "Unlimited team members",
      "Audit log",
      "Dedicated support",
      "SLA guarantee",
      "Custom integrations",
    ],
  },
];
