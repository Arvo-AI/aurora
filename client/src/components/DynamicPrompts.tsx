"use client"

import { useState, useEffect } from "react"

interface DynamicPromptsProps {
  onPromptClick: (prompt: string) => void
  className?: string
}

const promptSets = {
  compute: {
    title: "Compute & Clusters",
    prompts: [
      "Deploy a VM in AWS",
      "Create a Kubernetes cluster in GCP",
      "Set up a load balancer in AWS",
      "Create a serverless function in GCP",
      "Deploy a Docker container to Azure",
      "Set up auto-scaling in AWS for my cluster",
    ],
  },
  networking: {
    title: "Networking & Security",
    prompts: [
      "Reserve a static IP in GCP",
      "Create a VPC in AWS for a public‑facing web application spanning two Availability Zones.",
      "Help me set up a CDN in Azure",
      "Can you configure DNS routing in AWS",
      "Create a VPN connection in GCP",
      "Set up a bastion host in AWS",
      "Configure firewall rules in Azure so that only my app can access it",
      "Create network subnets in GCP",
    ],
  },
  storage: {
    title: "Storage & Databases",
    prompts: [
      "Create a storage bucket in GCP",
      "Set up a database in AWS",
      "Create automated backups in Azure",
      "Set up a data warehouse in GCP",
      "Configure file storage in AWS",
      "Create a Redis cache in Azure",
      "Set up data replication in GCP",
      "Configure object storage in AWS",
    ],
  },
  operations: {
    title: "Monitoring & Operations",
    prompts: [
      "Check the logs in my Kubernetes cluster in GCP",
      "Tell me what VMs I have running right now in AWS",
      "What resources do I have in the us-west-2 region in AWS",
      "Show me my current costs in Azure",
      "List all my databases in GCP",
      "Check my application performance in AWS",
      "Report to me my network traffic in Azure",
      "Monitor my CPU usage in GCP on my aurora instance",
    ],
  },
}

export default function DynamicPrompts({ onPromptClick, className = "" }: DynamicPromptsProps) {
  const [currentSet, setCurrentSet] = useState("compute")
  const [displayedPrompts, setDisplayedPrompts] = useState<string[]>([])
  const [isRotating, setIsRotating] = useState(false)

  // Rotate through different prompt sets
  useEffect(() => {
    const interval = setInterval(() => {
      const sets = Object.keys(promptSets)
      const currentIndex = sets.indexOf(currentSet)
      const nextIndex = (currentIndex + 1) % sets.length
      setCurrentSet(sets[nextIndex])
    }, 8000) // Change every 8 seconds

    return () => clearInterval(interval)
  }, [currentSet])

  // Update displayed prompts when set changes
  useEffect(() => {
    setIsRotating(true)
    setTimeout(() => {
      const prompts = promptSets[currentSet as keyof typeof promptSets].prompts
      // Show 2 random prompts from the current set (side by side)
      const shuffled = [...prompts].sort(() => 0.5 - Math.random())
      setDisplayedPrompts(shuffled.slice(0, 2))
      setIsRotating(false)
    }, 300)
  }, [currentSet])

  return (
    <div className={`space-y-4 ${className}`}>
      {/* Dynamic Prompts - Side by side */}
      <div
        className={`transition-all duration-500 ${isRotating ? "opacity-0 translate-y-2" : "opacity-100 translate-y-0"}`}
      >
        <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-2">
          {displayedPrompts.length > 0 ? (
            displayedPrompts.map((prompt, index) => (
              <button
                key={`${currentSet}-${index}`}
                onClick={() => onPromptClick(prompt)}
                className="text-left text-gray-700 dark:text-gray-300 hover:text-blue-400 transition-all duration-200 hover:translate-x-1 animate-in slide-in-from-left-2"
                style={{ animationDelay: `${index * 100}ms` }}
              >
                <span className="text-gray-500 mr-2">→</span>
                {prompt}
              </button>
            ))
          ) : (
            // Skeleton loading for prompts
            <>
              <div className="h-6 bg-muted animate-pulse rounded w-3/4" />
              <div className="h-6 bg-muted animate-pulse rounded w-2/3" />
            </>
          )}
        </div>
      </div>

      {/* Category Tabs */}
      <div className="flex flex-wrap gap-2 justify-center pt-2">
        {displayedPrompts.length > 0 ? (
          Object.entries(promptSets).map(([key, set]) => (
            <button
              key={key}
              onClick={() => setCurrentSet(key)}
              className={`px-3 py-1 rounded-full text-xs transition-all duration-200 ${
                currentSet === key
                  ? "bg-blue-600 text-white"
                  : "bg-gray-800 text-gray-400 hover:bg-gray-700 hover:text-gray-300"
              }`}
            >
              {set.title}
            </button>
          ))
        ) : (
          // Skeleton loading for category tabs
          <>
            <div className="h-6 bg-muted animate-pulse rounded-full w-24" />
            <div className="h-6 bg-muted animate-pulse rounded-full w-20" />
            <div className="h-6 bg-muted animate-pulse rounded-full w-16" />
            <div className="h-6 bg-muted animate-pulse rounded-full w-28" />
          </>
        )}
      </div>
    </div>
  )
} 