/**
 * AgentCeption — Alpine.js component library entry point
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
 * Module index:
 *   base.js         — batchBar
 *   nav.js          — projectSwitcher
 *   overview.js     — pipelineDashboard, agentCard, phaseSwitcher,
 *                     pipelineControl, sweepControl, waveControl,
 *                     conductorModal, runConductorPanel, scalingAdvisor,
 *                     prViolations, staleClaimCard, issueCard
 *   agents.js       — agentsPage, missionControl
 *   telemetry.js    — telemetryDash, waveTable
 *   dag.js          — dagVisualization
 *   config.js       — configPanel
 *   roles.js        — roleDetail, rolesEditor
 *   plan.js         — planForm
 *   build.js        — buildPage
 *   transcripts.js  — transcriptBrowser, transcriptDetail
 *   templates.js    — exportPanel, importPanel, envSandbox
 *   api.js          — apiEndpoint
 *   controls.js     — controlsKill
 *   toast.js        — toastStore
 */

'use strict';

import { batchBar } from './base.js';
import { projectSwitcher } from './nav.js';
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
import { planForm } from './plan.js';
import { buildPage } from './build.js';
import { transcriptBrowser, transcriptDetail } from './transcripts.js';
import { exportPanel, importPanel, envSandbox } from './templates.js';
import { apiEndpoint } from './api.js';
import { controlsKill } from './controls.js';
import { toastStore } from './toast.js';

// Expose all Alpine component factory functions globally so templates can
// reference them via x-data="functionName()" without any changes.
Object.assign(window, {
  batchBar,
  projectSwitcher,
  pipelineDashboard, agentCard, phaseSwitcher, pipelineControl,
  sweepControl, waveControl, conductorModal, scalingAdvisor, prViolations,
  staleClaimCard, issueCard, approvalCard, runConductorPanel,
  agentsPage, missionControl,
  telemetryDash, waveTable,
  dagVisualization,
  configPanel,
  roleDetail, rolesEditor,
  planForm,
  buildPage,
  transcriptBrowser, transcriptDetail,
  exportPanel, importPanel, envSandbox,
  apiEndpoint,
  controlsKill,
  toastStore,
});
