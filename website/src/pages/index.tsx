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
    title: 'Investigations',
    icon: '01',
    description:
      'An alert starts an agent. It runs kubectl and cloud CLIs in a sandbox and returns a root cause with the evidence.',
  },
  {
    title: 'Actions',
    icon: '02',
    description:
      'Postmortems, alert-gap audits, right-sizing, and your own instructions, on an incident, a schedule, or a click.',
  },
  {
    title: 'Monitor',
    icon: '03',
    description:
      'Fleet runs, SRE timings, tool usage, living artifacts the agent keeps current, and an audit log.',
  },
  {
    title: 'Your stack',
    icon: '04',
    description:
      'AWS, Azure, GCP, Kubernetes, Slack, Teams, ServiceNow, PagerDuty, Datadog, and the rest of the connector list.',
  },
  {
    title: 'Your model',
    icon: '05',
    description:
      'OpenRouter, OpenAI, Anthropic, Gemini, Vertex, Bedrock, or Ollama on your own machine.',
  },
  {
    title: 'Self-hosted',
    icon: '06',
    description:
      'Docker or Helm. Vault or AWS Secrets Manager for secrets. Command safety guardrails on by default. Apache 2.0.',
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
      description="Aurora investigates incidents, writes the postmortem, and keeps living artifacts. Self-hosted, Apache 2.0.">
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
