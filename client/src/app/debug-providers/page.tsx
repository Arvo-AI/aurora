import { redirect } from 'next/navigation';

export default function DebugProvidersPage() {
  // Redirect to the admin section
  redirect('/admin/debug-providers');
}
