import "./globals.css";
import ConditionalShell from "./components/ConditionalShell";
import { AuthProvider } from "./components/AuthProvider";

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
        <AuthProvider>
          <ConditionalShell>{children}</ConditionalShell>
        </AuthProvider>
      </body>
    </html>
  );
}
