import type { NextConfig } from "next";

// Bundle analyzer for performance measurement - only load if available
let withBundleAnalyzer: (config: NextConfig) => NextConfig;
try {
  withBundleAnalyzer = require('@next/bundle-analyzer')({
    enabled: process.env.ANALYZE === 'true',
  });
} catch (error) {
  // If @next/bundle-analyzer is not available, use identity function
  withBundleAnalyzer = (config: NextConfig) => config;
}

// Use server-side backend URL for rewrites and redirects (Docker service name).
// BACKEND_URL (aurora-server:5080) uses Docker DNS directly; NEXT_PUBLIC_BACKEND_URL
// (localhost:5080) goes through Docker NAT and is prone to stale connections.
const backendUrl = process.env.BACKEND_URL || process.env.NEXT_PUBLIC_BACKEND_URL || 'http://localhost:5080';

const nextConfig: NextConfig = {
  typescript: {
    ignoreBuildErrors: true,
  },
  eslint: {
    ignoreDuringBuilds: true,
  },
  cacheMaxMemorySize: 0,
  webpack: (config, { isServer, dev }) => {
    // Aggressive tree shaking configuration
    if (!isServer && !dev) {
      // Enable aggressive tree shaking
      config.optimization = {
        ...config.optimization,
        usedExports: true,
        sideEffects: false,
        splitChunks: {
          ...config.optimization.splitChunks,
          chunks: 'all',
          cacheGroups: {
            ...config.optimization.splitChunks?.cacheGroups,
            // Separate heavy libraries into their own chunks
            charts: {
              test: /[\\/]node_modules[\\/](recharts|d3-|victory-)[\\/]/,
              name: 'charts',
              chunks: 'all',
              priority: 20,
            },
            ui: {
              test: /[\\/]node_modules[\\/](@radix-ui|lucide-react|framer-motion)[\\/]/,
              name: 'ui-components',
              chunks: 'all', 
              priority: 15,
            },
            editor: {
              test: /[\\/]node_modules[\\/](@monaco-editor|monaco-editor)[\\/]/,
              name: 'monaco-editor',
              chunks: 'all',
              priority: 25,
            },
            vendor: {
              test: /[\\/]node_modules[\\/]/,
              name: 'vendor',
              chunks: 'all',
              priority: 10,
              minSize: 100000, // Only create chunk if > 100kB
            },
          },
        },
      };

      // Configure module resolution for better tree shaking  
      // Note: Don't alias lodash globally as it breaks recharts dependencies
    }

    // Handle Monaco Editor and JSZip
    if (!isServer) {
      config.resolve.fallback = {
        ...config.resolve.fallback,
        fs: false,
        path: false,
        crypto: false,
        stream: false,
        buffer: false,
        'web-worker': false,
      };
    }
    
    return config;
  },
  async headers() {
    return [
      {
        source: '/(.*)',
        headers: [
          // Tell browsers to close idle connections after 120s, well before
          // Docker Desktop's ~10min silent TCP drop window.
          { key: 'Keep-Alive', value: 'timeout=120' },
          { key: 'X-Frame-Options', value: 'DENY' },
          { key: 'X-Content-Type-Options', value: 'nosniff' },
          { key: 'Referrer-Policy', value: 'strict-origin-when-cross-origin' },
          { key: 'Permissions-Policy', value: 'camera=(), microphone=(), geolocation=()' },
          { key: 'Strict-Transport-Security', value: 'max-age=63072000; includeSubDomains; preload' },
          {
            key: 'Content-Security-Policy',
            value: [
              "default-src 'self'",
              "script-src 'self' 'unsafe-eval' 'unsafe-inline'",
              "style-src 'self' 'unsafe-inline'",
              "img-src 'self' data: blob: https:",
              "font-src 'self' data:",
              "connect-src 'self' ws: wss: http: https:",
              "frame-ancestors 'none'",
            ].join('; '),
          },
        ],
      },
    ];
  },
  async rewrites() {
    return [
      // Only rewrite /azure/ routes - all /api/ routes are handled by Next.js API routes which proxy to backend
      {
        source: "/azure/:path*",
        destination: `${backendUrl}/azure/:path*`,
      },
      {
        source: "/google-chat/events",
        destination: `${backendUrl}/google-chat/events`,
      },
    ];
  },
  async redirects() {
    return [
      {
        source: "/auth",
        destination: `${backendUrl}/auth`,
        permanent: false, // Use false for temporary redirects (e.g. login)
      },
    ];
  },
};

export default withBundleAnalyzer(nextConfig);
