/** @type {import('next').NextConfig} */
const API_URL = process.env.API_URL || 'http://127.0.0.1:3002';

const nextConfig = {
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: `${API_URL}/api/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;
