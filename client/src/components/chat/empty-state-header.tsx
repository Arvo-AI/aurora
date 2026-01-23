"use client";

import { useState } from "react";

// Task suggestion titles - kept modular and separate from main component
const TASK_TITLES = [
  "Deploying a Virtual Machine?",
  "Creating a Kubernetes Cluster?", 
  "Launching a Serverless App?",
  "Running a Cloud Function?",
  "Monitoring Cloud Costs?",
  "Securing Cloud Storage?",
  "Troubleshooting App Performance?",
  "Automating Resource Scaling?",
  "Managing Network Connectivity?",
  "Configuring Backup Solutions?",
  "Generating Infrastructure Code?",
  "Analyzing Resource Alerts?"
] as const;

interface EmptyStateHeaderProps {
  className?: string;
}

export default function EmptyStateHeader({ className = "" }: EmptyStateHeaderProps) {
  // Pick a random title once per page load - keeps logic contained
  const [suggestedTaskTitle] = useState<string>(() => {
    return TASK_TITLES[Math.floor(Math.random() * TASK_TITLES.length)];
  });

  return (
    <h2 className={`text-3xl font-semibold mb-10 text-muted-foreground ${className}`}>
      {suggestedTaskTitle}
    </h2>
  );
}
