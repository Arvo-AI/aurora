import "./globals.css";
import { SessionProvider } from "next-auth/react";
import ConditionalShell from "./components/ConditionalShell";

export const dynamic = 'force-dynamic';

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {

  return (
    <html lang="en" className="h-full" suppressHydrationWarning>
      <head>
        <script src="/env-config.js" />
      </head>
      <body className="antialiased h-full">
        <SessionProvider refetchOnWindowFocus={true}>
          <ConditionalShell>{children}</ConditionalShell>
        </SessionProvider>
      </body>
    </html>
  );
}
