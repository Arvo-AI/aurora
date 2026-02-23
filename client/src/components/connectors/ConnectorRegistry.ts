import { Github, Server } from "lucide-react";
import { isSlackEnabled, isOvhEnabled, isConfluenceEnabled } from "@/lib/feature-flags";
import type { ConnectorConfig } from "./types";

class ConnectorRegistry {
  private connectors: Map<string, ConnectorConfig> = new Map();

  constructor() {
    this.registerDefaultConnectors();
  }

  private registerDefaultConnectors() {
    // Infrastructure - register onprem first
    this.register({
      id: "onprem",
      name: "Instances SSH Access",
      description: "Manage SSH keys and configure virtual machines for on-premises and cloud infrastructure access.",
      icon: Server,
      iconColor: "text-foreground",
      iconBgColor: "bg-muted",
      category: "Infrastructure",
      path: "/vm-config",
      storageKey: "isOnPremConnected",
    });

    this.register({
      id: "grafana",
      name: "Grafana",
      description: "Monitor your infrastructure and receive real-time alerts from Grafana dashboards. Connect your Grafana instance to get instant notifications and insights.",
      iconPath: "/grafana.svg",
      iconBgColor: "bg-white dark:bg-white",
      category: "Monitoring",
      path: "/grafana/auth",
      storageKey: "isGrafanaConnected",
      alertsPath: "/grafana/alerts",
      alertsLabel: "View Alerts",
    });

    this.register({
      id: "datadog",
      name: "Datadog",
      description: "Bring Datadog logs, metrics, and monitor alerts into Aurora. Connect with your Datadog service account to centralize observability insights.",
      iconPath: "/datadog.svg",
      iconBgColor: "bg-white dark:bg-white",
      category: "Monitoring",
      path: "/datadog/auth",
      storageKey: "isDatadogConnected",
      alertsPath: "/datadog/events",
      alertsLabel: "View Events",
      overviewPath: "/datadog/overview",
      overviewLabel: "Overview",
    });

    this.register({
      id: "netdata",
      name: "Netdata",
      description: "Real-time infrastructure monitoring with Netdata Cloud. Receive alerts and monitor system metrics across all your nodes.",
      iconPath: "/netdata.svg",
      iconBgColor: "bg-white dark:bg-white",
      category: "Monitoring",
      path: "/netdata/auth",
      storageKey: "isNetdataConnected",
      alertsPath: "/netdata/alerts",
      alertsLabel: "View Alerts",
    });

    this.register({
      id: "splunk",
      name: "Splunk",
      description: "Connect to Splunk Cloud or Enterprise for log analytics, search, and security monitoring.",
      iconPath: "/splunk.svg",
      iconBgColor: "bg-white dark:bg-white",
      category: "Monitoring",
      path: "/splunk/auth",
      storageKey: "isSplunkConnected",
      alertsPath: "/splunk/alerts",
      alertsLabel: "View Alerts",
    });

    this.register({
      id: "dynatrace",
      name: "Dynatrace",
      description: "Connect to Dynatrace for full-stack observability. Receive problem notifications and query metrics, logs, and entities for root cause analysis.",
      iconPath: "/dynatrace.svg",
      iconBgColor: "bg-white dark:bg-white",
      category: "Monitoring",
      path: "/dynatrace/auth",
      storageKey: "isDynatraceConnected",
      alertsPath: "/dynatrace/alerts",
      alertsLabel: "View Problems",
    });

    this.register({
      id: "pagerduty",
      name: "PagerDuty",
      description: "Connect PagerDuty to receive incident alerts and manage on-call schedules. Integrate with your PagerDuty account for real-time incident management.",
      iconPath: "/pagerduty-icon.svg",
      iconBgColor: "bg-white dark:bg-white",
      category: "Incident Management",
      path: "/pagerduty/auth",
      storageKey: "isPagerDutyConnected",
    });

    if (isConfluenceEnabled()) {
      this.register({
        id: "confluence",
        name: "Confluence",
        description: "Fetch runbooks and documentation from Confluence pages to automate incident response workflows.",
        iconPath: "/confluence.svg",
        iconBgColor: "bg-white dark:bg-white",
        category: "Documentation",
        path: "/confluence/connect",
        storageKey: "isConfluenceConnected",
      });
    }

    this.register({
      id: "kubectl",
      name: "Kubernetes",
      description: "Deploy the Aurora agent into your Kubernetes cluster so Aurora can perform root cause analysis investigations securely.",
      iconPath: "/kubernetes-svgrepo-com.svg",
      iconBgColor: "bg-white dark:bg-white",
      category: "Infrastructure",
      path: "/kubectl/manage",
      storageKey: "isKubectlConnected",
    });

    this.register({
      id: "github",
      name: "GitHub",
      description: "Integrate with GitHub to manage repositories, track issues, and automate workflows. Connect your GitHub account to enable seamless code collaboration.",
      icon: Github,
      iconColor: "text-gray-800 dark:text-gray-300",
      iconBgColor: "bg-gray-200 dark:bg-gray-800",
      category: "Development",
      useCustomConnection: true,
    });

    if (isSlackEnabled()) {
      this.register({
        id: "slack",
        name: "Slack",
        description: "Receive alerts and notifications directly in your Slack workspace. Connect your Slack workspace to get real-time updates and interact with Aurora.",
        iconPath: "/slack.png",
        iconBgColor: "bg-white dark:bg-white",
        category: "Communication",
        storageKey: "isSlackConnected",
        useCustomConnection: true,
      });
    }

    // Cloud Providers (now under Infrastructure category)
    this.register({
      id: "gcp",
      name: "Google Cloud",
      description: "Connect to Google Cloud Platform to manage cloud resources, monitor services, and deploy applications across your GCP infrastructure.",
      iconPath: "/google-cloud-svgrepo-com.svg",
      iconBgColor: "bg-white dark:bg-white",
      category: "Infrastructure",
      storageKey: "isGCPConnected",
      useCustomConnection: true,
    });

    this.register({
      id: "azure",
      name: "Azure",
      description: "Connect to Microsoft Azure to manage cloud resources, monitor services, and deploy applications across your Azure infrastructure.",
      iconPath: "/azure.ico",
      iconBgColor: "bg-muted",
      category: "Infrastructure",
      path: "/azure/auth",
      storageKey: "isAzureConnected",
      useCustomConnection: true,
    });

    this.register({
      id: "aws",
      name: "AWS",
      description: "Connect to Amazon Web Services to manage cloud resources, monitor services, and deploy applications across your AWS infrastructure.",
      iconPath: "/aws.ico",
      iconBgColor: "bg-white dark:bg-white",
      category: "Infrastructure",
      path: "/aws/onboarding",
      storageKey: "isAWSConnected",
      useCustomConnection: true,
    });

    if (isOvhEnabled()) {
      this.register({
        id: "ovh",
        name: "OVH Cloud",
        description: "Connect to OVH Cloud to manage your Public Cloud projects, monitor resources, and deploy applications across OVH infrastructure.",
        iconPath: "/ovh.svg",
        iconBgColor: "bg-white dark:bg-white",
        category: "Infrastructure",
        path: "/ovh/onboarding",
        storageKey: "isOVHConnected",
        useCustomConnection: true,
      });
    }

    this.register({
      id: "scaleway",
      name: "Scaleway",
      description: "Connect to Scaleway to manage cloud resources, monitor services, and deploy applications across your Scaleway infrastructure.",
      iconPath: "/scaleway.svg",
      iconBgColor: "bg-muted",
      category: "Infrastructure",
      path: "/scaleway/onboarding",
      storageKey: "isScalewayConnected",
      useCustomConnection: true,
    });

    this.register({
      id: "tailscale",
      name: "Tailscale",
      description: "Connect to Tailscale to manage your private network, access resources securely, and configure networking across your infrastructure.",
      iconPath: "/tailscale.svg",
      iconBgColor: "bg-muted",
      category: "Networking",
      path: "/tailscale/onboarding",
      storageKey: "isTailscaleConnected",
    });
  }

  register(connector: ConnectorConfig): void {
    this.connectors.set(connector.id, connector);
  }

  get(id: string): ConnectorConfig | undefined {
    return this.connectors.get(id);
  }

  getAll(): ConnectorConfig[] {
    return Array.from(this.connectors.values());
  }

  getByCategory(category: string): ConnectorConfig[] {
    return this.getAll().filter(c => c.category === category);
  }
}

export const connectorRegistry = new ConnectorRegistry();
