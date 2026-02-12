import clsx from 'clsx';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import Heading from '@theme/Heading';
import styles from './index.module.css';

function HomepageHeader() {
  const {siteConfig} = useDocusaurusContext();
  return (
    <header className={styles.heroBanner}>
      <div className={styles.heroGlow} />
      <div className={styles.heroGrid} />
      <div className={clsx('container', styles.heroContent)}>
        <div className={styles.heroBadge}>
          <span className={styles.badgeDot} />
          Open Source
        </div>
        <Heading as="h1" className={styles.heroTitle}>
          {siteConfig.title}
        </Heading>
        <p className={styles.heroSubtitle}>{siteConfig.tagline}</p>
        <div className={styles.buttons}>
          <Link
            className={styles.buttonPrimary}
            to="/docs/getting-started/quickstart">
            <span>Get Started</span>
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
              <path d="M6 12L10 8L6 4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          </Link>
          <Link
            className={styles.buttonSecondary}
            to="/docs">
            Documentation
          </Link>
        </div>
      </div>
    </header>
  );
}

type FeatureItem = {
  title: string;
  description: string;
  icon: string;
};

const FeatureList: FeatureItem[] = [
  {
    title: 'AI-Powered RCA',
    icon: '01',
    description:
      'Automated root cause analysis using LLM agents to help SREs resolve incidents faster.',
  },
  {
    title: 'Multi-Cloud',
    icon: '02',
    description:
      'Connect to GCP, AWS, Azure, and more. Query infrastructure using natural language.',
  },
  {
    title: 'Local-First',
    icon: '03',
    description:
      'Run entirely on your machine with Docker. No cloud accounts required to start.',
  },
  {
    title: 'Extensible',
    icon: '04',
    description:
      'Integrate with Slack, PagerDuty, Datadog, Grafana, and other observability tools.',
  },
  {
    title: 'Secure',
    icon: '05',
    description:
      'HashiCorp Vault for secrets. Your credentials stay on your infrastructure.',
  },
  {
    title: 'Open Source',
    icon: '06',
    description:
      'Apache 2.0 licensed. Fully transparent, community-driven development.',
  },
];

function Feature({title, description, icon}: FeatureItem) {
  return (
    <div className={styles.featureCard}>
      <div className={styles.featureIcon}>{icon}</div>
      <Heading as="h3" className={styles.featureTitle}>{title}</Heading>
      <p className={styles.featureDescription}>{description}</p>
    </div>
  );
}

function QuickstartSection() {
  return (
    <section className={styles.quickstart}>
      <div className="container">
        <Heading as="h2" className={styles.sectionTitle}>
          Get started in minutes
        </Heading>
        <div className={styles.quickstartSteps}>
          <div className={styles.quickstartStep}>
            <span className={styles.stepNumber}>1</span>
            <div className={styles.stepContent}>
              <div className={styles.stepTitle}>Initialize</div>
              <div className={styles.stepDescription}>
                <code className={styles.stepCode}>make init</code>
              </div>
            </div>
          </div>
          <div className={styles.quickstartStep}>
            <span className={styles.stepNumber}>2</span>
            <div className={styles.stepContent}>
              <div className={styles.stepTitle}>Add your LLM API key</div>
              <div className={styles.stepDescription}>
                Edit <code className={styles.stepCode}>.env</code> and add your preferred LLM provider key
              </div>
            </div>
          </div>
          <div className={styles.quickstartStep}>
            <span className={styles.stepNumber}>3</span>
            <div className={styles.stepContent}>
              <div className={styles.stepTitle}>Start Aurora</div>
              <div className={styles.stepDescription}>
                <code className={styles.stepCode}>make prod-prebuilt</code> or <code className={styles.stepCode}>make prod-local</code>
              </div>
            </div>
          </div>
        </div>
        <div className={styles.quickstartCta}>
          <Link
            className={styles.buttonSecondary}
            to="/docs/getting-started/quickstart">
            Full quickstart guide
          </Link>
        </div>
      </div>
    </section>
  );
}

export default function Home(): JSX.Element {
  const {siteConfig} = useDocusaurusContext();
  return (
    <Layout
      title={`${siteConfig.title}`}
      description="Aurora is an automated root cause analysis tool that uses AI agents to help Site Reliability Engineers resolve incidents.">
      <HomepageHeader />
      <div className={styles.sectionDivider} />
      <QuickstartSection />
      <main className={styles.features}>
        <div className="container">
          <Heading as="h2" className={styles.sectionTitle}>
            Why Aurora?
          </Heading>
          <div className={styles.featuresGrid}>
            {FeatureList.map((props, idx) => (
              <Feature key={idx} {...props} />
            ))}
          </div>
        </div>
      </main>
    </Layout>
  );
}
