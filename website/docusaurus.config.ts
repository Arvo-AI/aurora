import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

const config: Config = {
  title: 'Aurora',
  tagline: 'AI-powered root cause analysis for Site Reliability Engineers',
  favicon: 'img/favicon.ico',

  // GitHub Pages deployment
  url: 'https://arvo-ai.github.io',
  baseUrl: '/aurora/',

  // GitHub Pages config
  organizationName: 'arvo-ai',
  projectName: 'aurora',
  trailingSlash: false,

  onBrokenLinks: 'throw',

  markdown: {
    format: 'mdx',
    mermaid: false,
    mdx1Compat: {
      comments: true,
      admonitions: true,
      headingIds: true,
    },
  },

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  presets: [
    [
      'classic',
      {
        docs: {
          sidebarPath: './sidebars.ts',
          editUrl: 'https://github.com/arvo-ai/aurora/tree/main/website/',
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themes: [
    [
      '@easyops-cn/docusaurus-search-local',
      {
        hashed: true,
        language: ['en'],
        highlightSearchTermsOnTargetPage: true,
        explicitSearchResultPath: true,
        docsRouteBasePath: '/docs',
        indexBlog: false,
      },
    ],
  ],

  themeConfig: {
    image: 'img/aurora-social-card.png',
    navbar: {
      title: 'Aurora',
      logo: {
        alt: 'Aurora Logo',
        src: 'img/logo.png',
      },
      items: [
        {
          type: 'docSidebar',
          sidebarId: 'docsSidebar',
          position: 'left',
          label: 'Docs',
        },
        {
          href: 'https://github.com/arvo-ai/aurora',
          label: 'GitHub',
          position: 'right',
        },
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Docs',
          items: [
            {
              label: 'Getting Started',
              to: '/docs/getting-started/quickstart',
            },
            {
              label: 'Configuration',
              to: '/docs/configuration/environment',
            },
            {
              label: 'Architecture',
              to: '/docs/architecture/overview',
            },
          ],
        },
        {
          title: 'Community',
          items: [
            {
              label: 'GitHub Discussions',
              href: 'https://github.com/arvo-ai/aurora/discussions',
            },
            {
              label: 'Contributing',
              href: 'https://github.com/arvo-ai/aurora/blob/main/CONTRIBUTING.md',
            },
          ],
        },
        {
          title: 'More',
          items: [
            {
              label: 'GitHub',
              href: 'https://github.com/arvo-ai/aurora',
            },
            {
              label: 'Changelog',
              href: 'https://github.com/arvo-ai/aurora/blob/main/CHANGELOG.md',
            },
          ],
        },
      ],
      copyright: `Copyright ${new Date().getFullYear()} Arvo AI. Apache License 2.0.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.okaidia,
      additionalLanguages: ['bash', 'yaml', 'json', 'python', 'typescript'],
    },
    colorMode: {
      defaultMode: 'dark',
      disableSwitch: false,
      respectPrefersColorScheme: true,
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
