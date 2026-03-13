import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    // jsdom gives us localStorage, ReadableStream, TextEncoder, etc.
    environment: 'jsdom',
    include: ['agentception/static/js/**/*.test.ts'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'html', 'lcov'],
      include: ['agentception/static/js/**/*.ts'],
      exclude: ['agentception/static/js/**/*.test.ts'],
    },
  },
});
