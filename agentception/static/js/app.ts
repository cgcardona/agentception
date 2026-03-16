/**
 * AgentCeption — Alpine.js component library entry point.
 *
 * All Alpine component factory functions live in domain-specific ES modules.
 * This entry point imports them all and assigns them to window so that
 * templates can reference them via x-data="functionName(args)" without any
 * changes to the HTML.
 *
 * Jinja2 data is injected at the call-site in the template attribute, never
 * inside this file.  This keeps the compiled output static, cacheable, and
 * free of server-side template rendering bugs caused by mismatched quote styles.
 *
 * Module index (✅ TypeScript, 🟡 JS — convert when page is reactivated):
 *   nav.ts          ✅ — projectSwitcher
 *   toast.ts        ✅ — toastStore
 *   controls.ts     ✅ — controlsKill
 *   build.ts        ✅ — buildPage, renderMd
 *   plan.ts         ✅ — planForm
 *   org_designer.ts ✅ — orgDesigner
 *   overview.js     🟡 — pipelineDashboard, agentCard, phaseSwitcher,
 *                         pipelineControl, sweepControl, waveControl,
 *                         conductorModal, runConductorPanel, scalingAdvisor,
 *                         prViolations, staleClaimCard, issueCard, approvalCard
 *   agents.js       🟡 — agentsPage, missionControl
 *   telemetry.js    🟡 — telemetryDash, waveTable
 *   dag.js          🟡 — dagVisualization
 *   config.js       🟡 — configPanel
 *   roles.js        🟡 — roleDetail, rolesEditor
 *   transcripts.js  🟡 — transcriptBrowser, transcriptDetail
 *   templates.js    🟡 — exportPanel, importPanel, envSandbox
 *   api.js          🟡 — apiEndpoint
 *   theme_toggle.ts ✅ — themeToggle
 */

'use strict';

// ── Converted TypeScript modules ─────────────────────────────────────────────
import { projectSwitcher } from './nav.ts';
import { toastStore } from './toast.ts';
import { controlsKill } from './controls.ts';
import { buildPage, renderMd } from './build.ts';
import { planForm } from './plan.ts';
import { orgDesigner } from './org_designer.ts';
import { themeToggle } from './theme_toggle.ts';

// ── Legacy JS modules (converted when their pages are reactivated) ───────────
import {
  pipelineDashboard, agentCard, phaseSwitcher, pipelineControl,
  sweepControl, waveControl, conductorModal, scalingAdvisor, prViolations,
  staleClaimCard, issueCard, approvalCard, runConductorPanel,
} from './overview.js';
import { agentsPage, missionControl } from './agents.js';
import { telemetryDash, waveTable } from './telemetry.js';
import { dagVisualization } from './dag.js';
import { configPanel } from './config.js';
import { roleDetail, rolesEditor } from './roles.js';
import { transcriptBrowser, transcriptDetail } from './transcripts.js';
import { exportPanel, importPanel, envSandbox } from './templates.js';
import { apiEndpoint } from './api.js';

// ── Global Alpine registration ───────────────────────────────────────────────
// Expose all Alpine component factory functions so templates can reference
// them via x-data="functionName()" without bundler integration in the HTML.
Object.assign(window as unknown as Record<string, unknown>, {
  // TypeScript modules
  projectSwitcher,
  toastStore,
  controlsKill,
  buildPage,
  renderMd,
  planForm,
  orgDesigner,
  themeToggle,
  // JS modules (untyped until converted)
  pipelineDashboard, agentCard, phaseSwitcher, pipelineControl,
  sweepControl, waveControl, conductorModal, scalingAdvisor, prViolations,
  staleClaimCard, issueCard, approvalCard, runConductorPanel,
  agentsPage, missionControl,
  telemetryDash, waveTable,
  dagVisualization,
  configPanel,
  roleDetail, rolesEditor,
  transcriptBrowser, transcriptDetail,
  exportPanel, importPanel, envSandbox,
  apiEndpoint,
});
