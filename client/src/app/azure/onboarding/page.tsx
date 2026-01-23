import { redirect } from 'next/navigation';

export default function AzureOnboardingRedirect() {
  // Temporary redirect while onboarding flow is disabled.
  redirect('/azure/auth');
}
