import { redirect } from 'next/navigation';

export default function AzureOnboardingRedirect() {
  redirect('/azure/auth');
}
