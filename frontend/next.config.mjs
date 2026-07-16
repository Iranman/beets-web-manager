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
    config.rewrites = async () => [
      {
        source: '/api/:path*',
        destination: 'http://192.168.0.250:8337/api/:path*',
      },
    ];
  }

  return config;
}