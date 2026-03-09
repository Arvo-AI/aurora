import "./globals.css";
import { SessionProvider } from "next-auth/react";
import ClientShell from "./components/ClientShell";
import ConditionalShell from "./components/ConditionalShell";

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
        <SessionProvider refetchInterval={60} refetchOnWindowFocus={true}>
          <ConditionalShell>{children}</ConditionalShell>
        </SessionProvider>
      </body>
    </html>
  );
}
