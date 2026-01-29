import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

const sidebars: SidebarsConfig = {
  docsSidebar: [
    {
      type: 'doc',
      id: 'index',
      label: 'Introduction',
    },
    {
      type: 'category',
      label: 'Getting Started',
      collapsed: false,
      items: [
        'getting-started/quickstart',
        'getting-started/dev-setup',
        'getting-started/prod-local',
      ],
    },
    {
      type: 'category',
      label: 'Configuration',
      items: [
        'configuration/environment',
        'configuration/vault',
        'configuration/storage',
      ],
    },
    {
      type: 'category',
      label: 'Deployment',
      items: [
        'deployment/docker-compose',
        'deployment/kubernetes',
        'deployment/vault-kms-setup',
        'deployment/vault-kms-gcp',
      ],
    },
    {
      type: 'category',
      label: 'Architecture',
      items: [
        'architecture/overview',
        'architecture/services',
      ],
    },
    {
      type: 'category',
      label: 'Integrations',
      items: [
        'integrations/connectors',
        'integrations/llm-providers',
      ],
    },
    {
      type: 'doc',
      id: 'troubleshooting',
      label: 'Troubleshooting',
    },
    {
      type: 'doc',
      id: 'faq',
      label: 'FAQ',
    },
  ],
};

export default sidebars;
