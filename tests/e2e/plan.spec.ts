/**
 * Playwright E2E tests for the Plan page — Phases 1A and 1B.
 *
 * Prerequisites
 * -------------
 * The AgentCeption stack must be running before these tests execute:
 *
 *   docker compose up -d
 *   npm run test:e2e
 *
 * External dependencies (OpenRouter LLM, GitHub gh CLI) are fully intercepted
 * via page.route() so no real API keys or network access are required.  The
 * tests exercise the real FastAPI server, real Jinja2 templates, real Alpine.js
 * component, and real HTMX — only the slow/external SSE endpoints are stubbed.
 *
 * Network mocking strategy
 * ------------------------
 * - POST /api/plan/preview  → fake SSE stream (avoids OpenRouter call)
 * - POST /api/plan/file-issues → fake SSE stream (avoids gh CLI)
 * - POST /api/plan/validate → hits the REAL backend (pure YAML computation)
 * - GET  /plan, /plan/recent-runs → hits the REAL backend
 *
 * This means the validate endpoint exercises the full Python stack including
 * Pydantic model validation, giving genuine confidence that the schema is
 * correct end-to-end.
 */

import { expect, Page, test } from '@playwright/test';

// ── SSE fixture helpers ────────────────────────────────────────────────────

const VALID_YAML = [
  'initiative: auth-rewrite',
  'phases:',
  '  - label: 0-foundation',
  '    description: "Add User model and migration"',
  '    depends_on: []',
  '    issues:',
  '      - id: auth-rewrite-p0-001',
  '        title: "Add SQLAlchemy User model"',
  '        body: "## Context\\nAdd a User model with id, email, hashed_password."',
].join('\n') + '\n';

const TWO_PHASE_YAML = [
  'initiative: auth-rewrite',
  'phases:',
  '  - label: 0-foundation',
  '    description: "Foundation"',
  '    depends_on: []',
  '    issues:',
  '      - id: auth-rewrite-p0-001',
  '        title: "Add SQLAlchemy User model"',
  '        body: "## Context\\nAdd a User model."',
  '      - id: auth-rewrite-p0-002',
  '        title: "Add Alembic migration"',
  '        body: "## Context\\nAdd migration."',
  '  - label: 1-api',
  '    description: "API layer"',
  '    depends_on: ["0-foundation"]',
  '    issues:',
  '      - id: auth-rewrite-p1-001',
  '        title: "Add login endpoint"',
  '        body: "## Context\\nAdd /auth/login."',
].join('\n') + '\n';

function buildPreviewSse(yaml: string, initiative = 'auth-rewrite', phases = 1, issues = 1): string {
  return [
    `data: {"t":"chunk","text":"${yaml.slice(0, 20).replace(/\n/g, '\\n')}"}\n`,
    `data: {"t":"done","yaml":${JSON.stringify(yaml)},"initiative":${JSON.stringify(initiative)},"phase_count":${phases},"issue_count":${issues}}\n`,
    '\n',
  ].join('\n');
}

const ISSUES_SINGLE_PHASE = [
  { issue_id: 'auth-rewrite-p0-001', number: 101, url: 'https://github.com/test/repo/issues/101', title: 'Add SQLAlchemy User model', phase: '0-foundation' },
  { issue_id: 'auth-rewrite-p0-002', number: 102, url: 'https://github.com/test/repo/issues/102', title: 'Add Alembic migration', phase: '0-foundation' },
  { issue_id: 'auth-rewrite-p1-001', number: 103, url: 'https://github.com/test/repo/issues/103', title: 'Add login endpoint', phase: '1-api' },
];

function buildFileIssuesSse(issues: typeof ISSUES_SINGLE_PHASE, batchId = 'batch-abc123'): string {
  const events: string[] = [
    `data: {"t":"start","total":${issues.length},"initiative":"auth-rewrite"}\n`,
    `data: {"t":"label","text":"Ensuring labels exist in GitHub\\u2026"}\n`,
  ];
  issues.forEach((iss, idx) => {
    events.push(
      `data: {"t":"issue","index":${idx + 1},"total":${issues.length},"number":${iss.number},"url":${JSON.stringify(iss.url)},"title":${JSON.stringify(iss.title)},"phase":${JSON.stringify(iss.phase)}}\n`,
    );
  });
  events.push(
    `data: {"t":"done","total":${issues.length},"batch_id":${JSON.stringify(batchId)},"initiative":"auth-rewrite","issues":${JSON.stringify(issues)},"coordinator_arch":{}}\n`,
    '\n',
  );
  return events.join('\n');
}

// ── Shared route-mocking helpers ──────────────────────────────────────────

async function mockPreview(page: Page, yaml = VALID_YAML, phases = 1, issues = 1): Promise<void> {
  await page.route('**/api/plan/preview', async route => {
    await route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
      body: buildPreviewSse(yaml, 'auth-rewrite', phases, issues),
    });
  });
}

async function mockFileIssues(page: Page, issues = ISSUES_SINGLE_PHASE): Promise<void> {
  await page.route('**/api/plan/file-issues', async route => {
    await route.fulfill({
      status: 200,
      headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
      body: buildFileIssuesSse(issues),
    });
  });
}

// ── Shared navigation helper ──────────────────────────────────────────────

async function goToPlan(page: Page): Promise<void> {
  await page.goto('/plan');
  await page.waitForLoadState('networkidle');
}

// ── Page load ─────────────────────────────────────────────────────────────

test.describe('Plan page — initial load', () => {
  test('loads in write state with textarea visible', async ({ page }) => {
    await goToPlan(page);
    await expect(page.locator('[x-ref="textarea"]')).toBeVisible();
    await expect(page.locator('button', { hasText: 'Generate plan' })).toBeVisible();
  });

  test('seed pills are visible and clickable', async ({ page }) => {
    await goToPlan(page);
    const firstSeed = page.locator('.plan-seed').first();
    await expect(firstSeed).toBeVisible();
    await firstSeed.click();
    // After clicking, the textarea should contain text.
    const value = await page.locator('[x-ref="textarea"]').inputValue();
    expect(value.length).toBeGreaterThan(0);
  });
});

// ── Phase 1A: Generate plan ───────────────────────────────────────────────

test.describe('Phase 1A — plan generation', () => {
  test('typing text and submitting shows generating state then review state', async ({ page }) => {
    await mockPreview(page);
    await goToPlan(page);

    await page.fill('[x-ref="textarea"]', 'Build user authentication with JWT tokens');
    await page.click('button:has-text("Generate plan")');

    // Generating state appears briefly.
    await expect(page.locator('#plan-generating')).toBeVisible({ timeout: 3000 });

    // Review state appears after SSE stream completes.
    await expect(page.locator('#plan-review')).toBeVisible({ timeout: 10_000 });
  });

  test('Cmd+Enter keyboard shortcut submits the form', async ({ page }) => {
    await mockPreview(page);
    await goToPlan(page);

    await page.fill('[x-ref="textarea"]', 'Build payment integration');
    await page.keyboard.press('Meta+Enter');

    await expect(page.locator('#plan-review')).toBeVisible({ timeout: 10_000 });
  });

  test('CodeMirror editor is present in review state', async ({ page }) => {
    await mockPreview(page);
    await goToPlan(page);
    await page.fill('[x-ref="textarea"]', 'Build auth');
    await page.click('button:has-text("Generate plan")');
    await expect(page.locator('#plan-review')).toBeVisible({ timeout: 10_000 });
    // CodeMirror renders a .cm-editor div.
    await expect(page.locator('.cm-editor')).toBeVisible();
  });

  test('phase/issue counts are shown in review state', async ({ page }) => {
    await mockPreview(page, TWO_PHASE_YAML, 2, 3);
    await goToPlan(page);
    await page.fill('[x-ref="textarea"]', 'Build auth with multiple phases');
    await page.click('button:has-text("Generate plan")');
    await expect(page.locator('#plan-review')).toBeVisible({ timeout: 10_000 });
    // Count display reflects the done event values (before live validation runs).
    const meta = page.locator('.plan-yaml-meta').first();
    await expect(meta).toContainText('2', { timeout: 5000 });
    await expect(meta).toContainText('3');
  });

  test('error SSE event shows error message and returns to write state', async ({ page }) => {
    await page.route('**/api/plan/preview', async route => {
      await route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
        body: 'data: {"t":"error","detail":"Input too vague — please add more detail."}\n\n',
      });
    });
    await goToPlan(page);
    await page.fill('[x-ref="textarea"]', '?');
    await page.click('button:has-text("Generate plan")');

    // Should land back on write state with the error visible.
    await expect(page.locator('#plan-write')).toBeVisible({ timeout: 10_000 });
    await expect(page.locator('.plan-error, [x-text="errorMsg"]')).toContainText(/vague|detail/i, { timeout: 5000 });
  });
});

// ── Review state: YAML editor + validation ────────────────────────────────

test.describe('Review state — YAML validation', () => {
  test.beforeEach(async ({ page }) => {
    await mockPreview(page);
    await goToPlan(page);
    await page.fill('[x-ref="textarea"]', 'Build auth');
    await page.click('button:has-text("Generate plan")');
    await expect(page.locator('#plan-review')).toBeVisible({ timeout: 10_000 });
  });

  test('valid YAML shows a green validation message', async ({ page }) => {
    // Wait for the validate call (triggered by CodeMirror mount) to settle.
    await expect(page.locator('.plan-yaml-status')).toContainText('Valid', { timeout: 8000 });
  });

  test('Launch button is enabled for valid YAML', async ({ page }) => {
    await expect(page.locator('.plan-yaml-status')).toContainText('Valid', { timeout: 8000 });
    await expect(page.locator('button:has-text("Launch")')).toBeEnabled();
  });

  test('"Edit plan" returns to write state', async ({ page }) => {
    await page.click('button:has-text("Edit plan")');
    await expect(page.locator('#plan-write')).toBeVisible();
  });
});

// ── Draft persistence ─────────────────────────────────────────────────────

test.describe('Draft persistence', () => {
  test('hard refresh restores review state from localStorage draft', async ({ page }) => {
    await mockPreview(page);
    await goToPlan(page);

    await page.fill('[x-ref="textarea"]', 'Build auth');
    await page.click('button:has-text("Generate plan")');
    await expect(page.locator('#plan-review')).toBeVisible({ timeout: 10_000 });

    // Hard refresh — the draft is stored in localStorage.
    await mockPreview(page);   // re-install mock for next navigation
    await page.reload();
    await page.waitForLoadState('networkidle');

    // Should restore to review, not write.
    await expect(page.locator('#plan-review')).toBeVisible({ timeout: 8000 });
    await expect(page.locator('.cm-editor')).toBeVisible();
  });
});

// ── Phase 1B: File issues ─────────────────────────────────────────────────

test.describe('Phase 1B — file issues', () => {
  test.beforeEach(async ({ page }) => {
    await mockPreview(page, TWO_PHASE_YAML, 2, 3);
    await goToPlan(page);
    await page.fill('[x-ref="textarea"]', 'Build auth');
    await page.click('button:has-text("Generate plan")');
    await expect(page.locator('#plan-review')).toBeVisible({ timeout: 10_000 });
    await expect(page.locator('.plan-yaml-status')).toContainText('Valid', { timeout: 8000 });
  });

  test('launching shows progress and transitions to done state', async ({ page }) => {
    await mockFileIssues(page);
    await page.click('button:has-text("Launch")');

    // Launching state briefly visible.
    await expect(page.locator('#plan-launching')).toBeVisible({ timeout: 5000 });

    // Done state after stream completes.
    await expect(page.locator('#plan-done')).toBeVisible({ timeout: 10_000 });
  });

  test('done state displays the batch ID', async ({ page }) => {
    await mockFileIssues(page);
    await page.click('button:has-text("Launch")');
    await expect(page.locator('#plan-done')).toBeVisible({ timeout: 10_000 });
    await expect(page.locator('.plan-done-batch-id')).toContainText('batch-abc123');
  });

  test('done state shows GitHub issue links', async ({ page }) => {
    await mockFileIssues(page);
    await page.click('button:has-text("Launch")');
    await expect(page.locator('#plan-done')).toBeVisible({ timeout: 10_000 });
    // Each created issue should have a link.
    const issueLinks = page.locator('.plan-done-issue a, #plan-done a[href*="github.com"]');
    await expect(issueLinks.first()).toBeVisible({ timeout: 5000 });
  });

  test('error during launch shows error message and returns to review', async ({ page }) => {
    await page.route('**/api/plan/file-issues', async route => {
      await route.fulfill({
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
        body: 'data: {"t":"start","total":3,"initiative":"auth-rewrite"}\n\ndata: {"t":"error","detail":"GitHub rate limited."}\n\n',
      });
    });
    await page.click('button:has-text("Launch")');
    await expect(page.locator('#plan-review')).toBeVisible({ timeout: 10_000 });
    await expect(page.locator('.plan-error, [x-text="errorMsg"]')).toContainText(/rate limited/i, { timeout: 5000 });
  });

  test('"New plan" button from done state resets to write', async ({ page }) => {
    await mockFileIssues(page);
    await page.click('button:has-text("Launch")');
    await expect(page.locator('#plan-done')).toBeVisible({ timeout: 10_000 });
    await page.click('button:has-text("New plan")');
    await expect(page.locator('#plan-write')).toBeVisible({ timeout: 5000 });
    await expect(page.locator('[x-ref="textarea"]')).toHaveValue('');
  });
});
