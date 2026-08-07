import { PHASE_DEVELOPMENT_SERVER } from 'next/constants.js';

/** @type {import('next').NextConfig} */
const baseConfig = {
  images: {
    unoptimized: true,
  },
};

export default function nextConfig(phase) {
  const isDev = phase === PHASE_DEVELOPMENT_SERVER;
  const config = {
    ...baseConfig,
    ...(isDev ? {} : { output: 'export' }),
  };

  if (isDev) {
    // Points at a running beets-web-manager backend during local frontend
    // development. Defaults to localhost; override with DEV_API_PROXY_TARGET
    // (e.g. a LAN host) if the backend isn't running on this machine.
    const target = process.env.DEV_API_PROXY_TARGET || 'http://localhost:8337';
    config.rewrites = async () => [
      {
        source: '/api/:path*',
        destination: `${target}/api/:path*`,
      },
    ];
  }

  return config;
}