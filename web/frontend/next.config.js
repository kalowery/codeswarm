/** @type {import('next').NextConfig} */
const nextConfig = {
  webpack: (config, { dev }) => {
    if (dev) {
      const rawIgnored = Array.isArray(config.watchOptions?.ignored)
        ? config.watchOptions.ignored
        : config.watchOptions?.ignored
          ? [config.watchOptions.ignored]
          : [];
      const ignored = rawIgnored.filter(
        (value) => typeof value === 'string' && value.trim().length > 0
      );
      config.watchOptions = {
        ...config.watchOptions,
        ignored: [
          ...ignored,
          '**/.git/**',
          '**/.next/**',
          '**/node_modules/**',
          '**/runs/**',
          '**/router_state.json',
          '**/log.txt',
          '**/web/backend/state.json',
        ],
      };
    }
    return config;
  },
};

module.exports = nextConfig;
