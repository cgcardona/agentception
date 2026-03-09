import { defineConfig, devices } from '@playwright/test';

/**
 * Playwright E2E configuration.
 *
 * Targets the AgentCeption server running in Docker at localhost:10003.
 * Start the stack first:  docker compose up -d
 *
 * Run all E2E tests:      npm run test:e2e
 * Run with UI explorer:   npm run test:e2e:ui
 *
 * All external dependencies (Anthropic API, GitHub) are intercepted via
 * page.route() in each test — no real API keys are required.
 */
export default defineConfig({
  testDir: './agentception/tests/e2e',
  timeout: 30_000,
  // Retry flaky tests once in CI; never locally (fail fast for dev).
  retries: process.env['CI'] ? 2 : 0,
  // Run tests sequentially to avoid port contention with the shared server.
  workers: process.env['CI'] ? 1 : undefined,
  reporter: [['html', { open: 'never' }], ['list']],
  use: {
    baseURL: 'http://localhost:10003',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
