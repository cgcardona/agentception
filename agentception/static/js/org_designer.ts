/**
 * Org Designer — visual tree builder for agent hierarchies.
 *
 * Architecture
 * ────────────
 * D3 v7 (CDN) owns the canvas: tree layout math, SVG bezier edges, and
 * absolutely-positioned HTML node cards.  Alpine.js owns only the overlay
 * open/close, node editor side panel, and submission state.
 *
 * Tree data is a plain mutable object tree (NOT Alpine-reactive) to avoid
 * recursion limits.  D3 re-renders the entire canvas on every mutation.
 *
 * Features
 * ────────
 * • Context-aware child filtering — parent role constrains available children
 * • Full org tree serialised into the DB context row at dispatch time
 * • localStorage persistence — tree survives page refreshes
 * • Initiative scope per coordinator node (full / specific phase)
 * • Launch feedback — root card turns green with run_id after dispatch
 */

'use strict';

// ── D3 minimal type declarations ──────────────────────────────────────────────
// D3 v7 is loaded from CDN.  We declare only the subset we use here, avoiding
// a runtime dependency on @types/d3 while remaining fully typed.

interface D3HierarchyNode {
  data: OrgNode;
  x: number;
  y: number;
  depth: number;
  links(): D3HierarchyLink[];
  descendants(): D3HierarchyNode[];
  each(callback: (node: D3HierarchyNode) => void): void;
}

interface D3HierarchyLink {
  source: D3HierarchyNode;
  target: D3HierarchyNode;
}

interface D3Selection {
  selectAll(selector: string): D3Selection;
  data(data: D3HierarchyNode[], key?: (d: D3HierarchyNode) => string): D3Selection;
  join(enter: string): D3Selection;
  enter(): D3Selection;
  append(tag: string): D3Selection;
  merge(other: D3Selection): D3Selection;
  exit(): D3Selection;
  remove(): void;
  attr(name: string, value: string | number | ((d: D3HierarchyNode) => string | number)): this;
  style(name: string, value: string | ((d: D3HierarchyNode) => string)): this;
  classed(names: string, value: boolean | ((d: D3HierarchyNode) => boolean)): this;
  html(value: string | ((d: D3HierarchyNode) => string)): this;
  node(): HTMLElement;
}

interface D3SVGSelection {
  attr(name: string, value: string | number): this;
  selectAll(selector: string): D3Selection;
}

interface D3LinkVertical {
  x(fn: (d: D3HierarchyNode) => number): this;
  y(fn: (d: D3HierarchyNode) => number): this;
  (link: D3HierarchyLink): string;
}

interface D3TreeLayout {
  nodeSize(size: [number, number]): this;
  (root: D3HierarchyNode): void;
}

interface D3Lib {
  hierarchy(root: OrgNode, children?: (d: OrgNode) => OrgNode[] | null): D3HierarchyNode;
  // Generic overload used by the live tree renderer.
  hierarchy<T>(root: T, children?: (d: T) => T[] | null): D3HierarchyNode;
  tree(): D3TreeLayout;
  tree<T>(): D3TreeLayout;
  select(el: Element): D3SVGSelection;
  linkVertical(): D3LinkVertical;
}

declare global {
  interface Window { d3: D3Lib }
}

// ── Domain types ──────────────────────────────────────────────────────────────

/** A single node in the designed org tree. */
interface OrgNode {
  id: string;
  role: string;
  figure: string;
  /** Scope for this node's dispatch target. */
  scope: 'full_initiative' | 'phase' | 'issue';
  /** Phase sub-label when scope === 'phase'. */
  scopeLabel: string;
  /** GitHub issue number when scope === 'issue'. */
  scopeIssueNumber: number | null;
  /** Set to true after a successful dispatch. */
  launched: boolean;
  /** run_id from a successful dispatch response. */
  runId: string;
  children: OrgNode[];
}

/** Serialised form sent to the backend. */
interface OrgNodePayload {
  id: string;
  role: string;
  figure: string;
  scope: 'full_initiative' | 'phase' | 'issue';
  scope_label: string;
  scope_issue_number: number | null;
  children: OrgNodePayload[];
}

/** Cognitive architecture figure from the backend catalog. */
export interface FigureItem {
  id: string;
  name: string;
}

/** Preset summary returned by GET /api/org-presets (no tree). */
interface ApiPresetSummary {
  id: string;
  name: string;
  description: string;
  icon: string;
  accent: string;
  node_count: number;
  group: string;
}

/** Full preset returned by GET /api/org-presets/{id}. */
interface ApiPresetDetail extends ApiPresetSummary {
  template: PresetTemplate;
}

/** Phase entry from GET /api/dispatch/context. */
interface PhaseItem {
  label: string;
  count: number;
  blocked: boolean;
}

interface IssueItem {
  number: number;
  title: string;
  blocked: boolean;
}

interface ContextResponse {
  phases: PhaseItem[];
  issues: IssueItem[];
}

interface DispatchResponse {
  run_id: string;
  batch_id: string;
  tier: string;
  role: string;
  label: string;
  worktree: string;
  host_worktree: string;
  status: string;
}

interface FastApiValidationError {
  msg: string;
  loc: string[];
  type: string;
}

interface DispatchError {
  // FastAPI returns detail as a string for application errors,
  // or as an array of validation error objects for 422 responses.
  detail?: string | FastApiValidationError[];
}

// ── Live mode types (from GET /api/org/batches and SSE /api/org/live) ─────────

/** One node from the DB agent-run tree (flat list, assembled client-side). */
interface RunTreeNodeRow {
  id: string;
  role: string;
  status: string;
  agent_status: string;
  tier: string | null;
  org_domain: string | null;
  parent_run_id: string | null;
  issue_number: number | null;
  pr_number: number | null;
  batch_id: string | null;
  spawned_at: string;
  last_activity_at: string | null;
  current_step: string | null;
}

/** Summary of one dispatch batch from GET /api/org/batches. */
interface BatchSummaryRow {
  batch_id: string;
  spawned_at: string;
  total_count: number;
  active_count: number;
}

/** Internal tree node used for D3 live-mode rendering. */
interface LiveOrgNode {
  data: RunTreeNodeRow;
  children: LiveOrgNode[];
}

/** SSE tree event from /api/org/live. */
interface LiveTreeEvent {
  t: 'tree';
  nodes: RunTreeNodeRow[];
  batch_id: string;
}

/** SSE idle event — no active batch. */
interface LiveIdleEvent {
  t: 'idle';
}

/** SSE ping keepalive. */
interface LivePingEvent {
  t: 'ping';
}

type LiveSseEvent = LiveTreeEvent | LiveIdleEvent | LivePingEvent;

// ── Role catalog ──────────────────────────────────────────────────────────────

interface RoleEntry {
  slug: string;
  label: string;
}

interface RoleGroup {
  label: string;
  type: 'coordinator' | 'worker';
  roles: RoleEntry[];
}

const ROLE_GROUPS: RoleGroup[] = [
  {
    label: 'C-Suite',
    type: 'coordinator',
    roles: [
      { slug: 'ceo',  label: 'CEO' },
      { slug: 'cto',  label: 'CTO' },
      { slug: 'cpo',  label: 'CPO' },
      { slug: 'coo',  label: 'COO' },
      { slug: 'cfo',  label: 'CFO' },
      { slug: 'ciso', label: 'CISO' },
      { slug: 'cmo',  label: 'CMO' },
      { slug: 'csto', label: 'CSTO' },
      { slug: 'cdo',  label: 'CDO' },
    ],
  },
  {
    label: 'Coordinators',
    type: 'coordinator',
    roles: [
      { slug: 'engineering-coordinator',     label: 'Engineering Manager' },
      { slug: 'qa-coordinator',              label: 'QA Lead' },
      { slug: 'ml-coordinator',              label: 'ML Coordinator' },
      { slug: 'design-coordinator',          label: 'Design Lead' },
      { slug: 'security-coordinator',        label: 'Security Lead' },
      { slug: 'platform-coordinator',        label: 'Platform Coordinator' },
      { slug: 'infrastructure-coordinator',  label: 'Infrastructure Lead' },
      { slug: 'data-coordinator',            label: 'Data Coordinator' },
      { slug: 'mobile-coordinator',          label: 'Mobile Lead' },
      { slug: 'product-coordinator',         label: 'Product Coordinator' },
    ],
  },
  {
    label: 'Engineering',
    type: 'worker',
    roles: [
      { slug: 'python-developer',      label: 'Python Developer' },
      { slug: 'typescript-developer',  label: 'TypeScript Developer' },
      { slug: 'go-developer',          label: 'Go Developer' },
      { slug: 'rust-developer',        label: 'Rust Developer' },
      { slug: 'rails-developer',       label: 'Rails Developer' },
      { slug: 'systems-programmer',    label: 'Systems Programmer' },
      { slug: 'api-developer',         label: 'API Developer' },
      { slug: 'full-stack-developer',  label: 'Full-Stack Developer' },
      { slug: 'data-engineer',         label: 'Data Engineer' },
      { slug: 'database-architect',    label: 'Database Architect' },
      { slug: 'architect',             label: 'Architect' },
    ],
  },
  {
    label: 'Frontend / Mobile',
    type: 'worker',
    roles: [
      { slug: 'frontend-developer',  label: 'Frontend Developer' },
      { slug: 'react-developer',     label: 'React Developer' },
      { slug: 'ios-developer',       label: 'iOS Developer' },
      { slug: 'android-developer',   label: 'Android Developer' },
      { slug: 'mobile-developer',    label: 'Mobile Developer' },
    ],
  },
  {
    label: 'ML / Data Science',
    type: 'worker',
    roles: [
      { slug: 'ml-engineer',    label: 'ML Engineer' },
      { slug: 'ml-researcher',  label: 'ML Researcher' },
      { slug: 'data-scientist', label: 'Data Scientist' },
    ],
  },
  {
    label: 'QA / Ops',
    type: 'worker',
    roles: [
      { slug: 'pr-reviewer',               label: 'PR Reviewer' },
      { slug: 'test-engineer',             label: 'Test Engineer' },
      { slug: 'devops-engineer',           label: 'DevOps Engineer' },
      { slug: 'site-reliability-engineer', label: 'SRE' },
    ],
  },
  {
    label: 'Other',
    type: 'worker',
    roles: [
      { slug: 'security-engineer', label: 'Security Engineer' },
      { slug: 'technical-writer',  label: 'Technical Writer' },
      { slug: 'content-writer',    label: 'Content Writer' },
    ],
  },
];

// ── Child role filtering ──────────────────────────────────────────────────────
// Encodes the org hierarchy: which roles a given parent is allowed to spawn.
// null = all slugs of that type are valid children.
// Empty Set = no children of that type are valid (e.g. workers can't spawn coordinators).

interface ChildRules {
  coordinator: Set<string> | null;
  worker: Set<string> | null;
}

const COORDINATOR_SLUGS = new Set<string>([
  'ceo', 'cto', 'cpo', 'coo', 'cfo', 'ciso', 'cmo', 'csto', 'cdo',
  'engineering-coordinator', 'qa-coordinator', 'ml-coordinator',
  'design-coordinator', 'security-coordinator', 'platform-coordinator',
  'infrastructure-coordinator', 'data-coordinator', 'mobile-coordinator',
  'product-coordinator', 'coordinator', 'conductor',
]);

function isCoordinator(slug: string): boolean {
  return COORDINATOR_SLUGS.has(slug);
}

const CHILD_ROLE_RULES: Record<string, ChildRules> = {
  // C-Suite — each executive owns their domain of coordinators
  ceo:  { coordinator: null, worker: null },
  cto:  {
    coordinator: new Set(['engineering-coordinator', 'qa-coordinator', 'ml-coordinator',
      'security-coordinator', 'platform-coordinator', 'infrastructure-coordinator', 'data-coordinator']),
    worker: null,
  },
  cpo:  {
    coordinator: new Set(['product-coordinator', 'design-coordinator', 'mobile-coordinator']),
    worker: null,
  },
  coo:  { coordinator: null, worker: null },
  cfo:  {
    coordinator: new Set(['data-coordinator']),
    worker: new Set(['data-scientist', 'data-engineer']),
  },
  ciso: {
    coordinator: new Set(['security-coordinator']),
    worker: new Set(['security-engineer']),
  },
  cmo:  {
    coordinator: new Set(['product-coordinator', 'design-coordinator']),
    worker: new Set(['content-writer', 'technical-writer']),
  },
  csto: {
    coordinator: new Set(['engineering-coordinator', 'platform-coordinator', 'infrastructure-coordinator']),
    worker: new Set(['architect', 'systems-programmer']),
  },
  cdo:  {
    coordinator: new Set(['data-coordinator', 'ml-coordinator']),
    worker: new Set(['data-scientist', 'data-engineer', 'ml-engineer']),
  },

  // Domain coordinators — each owns their specific leaf workers
  'engineering-coordinator': {
    coordinator: new Set([]),
    worker: new Set(['python-developer', 'typescript-developer', 'go-developer', 'rust-developer',
      'rails-developer', 'api-developer', 'full-stack-developer', 'systems-programmer',
      'data-engineer', 'database-architect', 'architect', 'react-developer', 'frontend-developer']),
  },
  'qa-coordinator':             { coordinator: new Set([]), worker: new Set(['pr-reviewer', 'test-engineer']) },
  'ml-coordinator':             { coordinator: new Set([]), worker: new Set(['ml-engineer', 'ml-researcher', 'data-scientist']) },
  'design-coordinator':         { coordinator: new Set([]), worker: new Set(['frontend-developer', 'react-developer', 'technical-writer', 'content-writer']) },
  'security-coordinator':       { coordinator: new Set([]), worker: new Set(['security-engineer', 'test-engineer']) },
  'platform-coordinator':       { coordinator: new Set([]), worker: new Set(['devops-engineer', 'site-reliability-engineer']) },
  'infrastructure-coordinator': { coordinator: new Set([]), worker: new Set(['systems-programmer', 'devops-engineer', 'site-reliability-engineer']) },
  'data-coordinator':           { coordinator: new Set([]), worker: new Set(['data-engineer', 'data-scientist']) },
  'mobile-coordinator':         { coordinator: new Set([]), worker: new Set(['ios-developer', 'android-developer', 'mobile-developer']) },
  'product-coordinator':        { coordinator: new Set([]), worker: new Set(['technical-writer', 'content-writer']) },
  coordinator:                  { coordinator: null, worker: null },
  conductor:                    { coordinator: null, worker: null },

  // Workers — can only spawn a PR Reviewer (for self-initiated code review)
};

const WORKER_RULES: ChildRules = { coordinator: new Set([]), worker: new Set(['pr-reviewer']) };

/** Returns the allowed child slugs for (parentRole, childType), or null for "all". */
function getChildRules(parentRole: string, childType: 'coordinator' | 'worker'): Set<string> | null {
  const rules = CHILD_ROLE_RULES[parentRole] ?? WORKER_RULES;
  return rules[childType] ?? new Set();
}

/** Which type tabs (coordinator/worker) are valid for a child of parentRole. */
function availableChildTypes(parentRole: string): Array<'coordinator' | 'worker'> {
  const rules = CHILD_ROLE_RULES[parentRole] ?? WORKER_RULES;
  const types: Array<'coordinator' | 'worker'> = [];
  if (rules.coordinator === null || (rules.coordinator as Set<string>).size > 0) types.push('coordinator');
  if (rules.worker === null || (rules.worker as Set<string>).size > 0) types.push('worker');
  return types;
}

/** Filter ROLE_GROUPS to roles valid for a child of parentRole in childType. */
function filterGroupsForParent(
  groups: RoleGroup[],
  parentRole: string | null,
  childType: 'coordinator' | 'worker',
): RoleGroup[] {
  const byType = groups.filter(g => g.type === childType);
  if (!parentRole) return byType;
  const allowed = getChildRules(parentRole, childType);
  if (allowed === null) return byType;
  return byType
    .map(g => ({ ...g, roles: g.roles.filter(r => (allowed as Set<string>).has(r.slug)) }))
    .filter(g => g.roles.length > 0);
}

/** Human-readable label for a role slug. */
function roleLabel(slug: string): string {
  for (const group of ROLE_GROUPS) {
    const match = group.roles.find(r => r.slug === slug);
    if (match) return match.label;
  }
  return slug;
}

// ── Tree node helpers ─────────────────────────────────────────────────────────

let _nodeCounter = 0;

function makeNode(role = '', figure = ''): OrgNode {
  return { id: `n${++_nodeCounter}`, role, figure, scope: 'full_initiative', scopeLabel: '', scopeIssueNumber: null, launched: false, runId: '', children: [] };
}

function findNode(node: OrgNode, id: string): OrgNode | null {
  if (node.id === id) return node;
  for (const child of node.children) {
    const found = findNode(child, id);
    if (found) return found;
  }
  return null;
}

function pruneNode(root: OrgNode, id: string): void {
  root.children = root.children.filter(c => c.id !== id);
  root.children.forEach(c => pruneNode(c, id));
}

function countNodes(node: OrgNode): number {
  return 1 + node.children.reduce((s, c) => s + countNodes(c), 0);
}

/** Serialise a node for the backend (snake_case keys, no UI-only fields). */
function serializeNode(node: OrgNode): OrgNodePayload {
  return {
    id:                node.id,
    role:              node.role,
    figure:            node.figure,
    scope:             node.scope,
    scope_label:       node.scopeLabel,
    scope_issue_number: node.scopeIssueNumber,
    children:          node.children.map(serializeNode),
  };
}

/** Restore a node from localStorage JSON, resetting launch state. */
function restoreNode(raw: Partial<OrgNode>): OrgNode {
  const id = raw.id ?? `n${++_nodeCounter}`;
  const num = parseInt(id.replace('n', ''), 10);
  if (!isNaN(num) && num >= _nodeCounter) _nodeCounter = num + 1;
  return {
    id,
    role:             raw.role ?? '',
    figure:           raw.figure ?? '',
    scope:            raw.scope ?? 'full_initiative',
    scopeLabel:       raw.scopeLabel ?? '',
    scopeIssueNumber: raw.scopeIssueNumber ?? null,
    launched:         false,   // always reset on restore — dispatches don't survive refresh
    runId:            '',
    children:         (raw.children ?? []).map(c => restoreNode(c as Partial<OrgNode>)),
  };
}

// ── localStorage helpers ──────────────────────────────────────────────────────

function storageKey(repo: string, initiative: string): string {
  return `ac_org_${repo}_${initiative}`;
}

function saveToStorage(repo: string, initiative: string, root: OrgNode): void {
  try {
    localStorage.setItem(storageKey(repo, initiative), JSON.stringify(root));
  } catch {
    // Quota exceeded — non-critical, ignore.
  }
}

function loadFromStorage(repo: string, initiative: string): OrgNode | null {
  try {
    const raw = localStorage.getItem(storageKey(repo, initiative));
    return raw ? restoreNode(JSON.parse(raw) as Partial<OrgNode>) : null;
  } catch {
    return null;
  }
}

function clearStorage(repo: string, initiative: string): void {
  try { localStorage.removeItem(storageKey(repo, initiative)); } catch { /* ignore */ }
}

// ── Live mode D3 helpers ───────────────────────────────────────────────────────

/** Build a hierarchical tree from a flat RunTreeNodeRow list. */
function buildLiveTree(nodes: RunTreeNodeRow[]): LiveOrgNode | null {
  if (!nodes.length) return null;
  const byId = new Map<string, LiveOrgNode>();
  nodes.forEach(n => byId.set(n.id, { data: n, children: [] }));

  let root: LiveOrgNode | null = null;
  nodes.forEach(n => {
    if (n.parent_run_id && byId.has(n.parent_run_id)) {
      byId.get(n.parent_run_id)!.children.push(byId.get(n.id)!);
    } else if (!root) {
      root = byId.get(n.id) ?? null;
    }
  });
  return root;
}

/** Return a CSS class name for an agent_status value. */
function liveStatusClass(agentStatus: string): string {
  const map: Record<string, string> = {
    implementing: 'od-live__chip--implementing',
    reviewing:    'od-live__chip--reviewing',
    done:         'od-live__chip--done',
    stale:        'od-live__chip--stale',
    cancelled:    'od-live__chip--cancelled',
    blocked:      'od-live__chip--blocked',
  };
  return map[agentStatus] ?? 'od-live__chip--pending';
}

/** HTML card for one live RunTreeNodeRow. */
function liveNodeCardHtml(row: RunTreeNodeRow, repo: string, initiative: string): string {
  const tierLabel = row.tier ? row.tier.toUpperCase() : 'AGENT';
  const roleLabel_ = roleLabel(row.role);
  const statusChip = `<span class="od-live__chip ${liveStatusClass(row.agent_status)}">${row.agent_status}</span>`;
  const step = row.current_step
    ? `<div class="od-live__step" title="${row.current_step}">↳ ${row.current_step.slice(0, 40)}</div>`
    : '';
  const issueLink = row.issue_number
    ? `<a class="od-live__issue-link" href="/ship/${encodeURIComponent(repo)}/${encodeURIComponent(initiative)}?run=${encodeURIComponent(row.id)}" target="_blank">#${row.issue_number}</a>`
    : '';
  const runIdTrunc = row.id.length > 14 ? row.id.slice(0, 14) + '…' : row.id;
  return `
    <div class="od-live__tier">${tierLabel}</div>
    <div class="od-live__role">${roleLabel_} ${statusChip}</div>
    <div class="od-live__run-id">${runIdTrunc}${issueLink}</div>
    ${step}`;
}

// Separate SVG/card layer maps for the live canvas.
const _liveSvgMap  = new Map<HTMLElement, D3SVGSelection>();
const _liveCardMap = new Map<HTMLElement, D3Selection>();

/** Render the live agent tree into *container* using D3. */
function renderLiveD3(
  nodes: RunTreeNodeRow[],
  container: HTMLElement,
  repo: string,
  initiative: string,
): void {
  const root = buildLiveTree(nodes);
  if (!root) {
    container.innerHTML = '<div class="od-live__empty">No agents in this batch yet.</div>';
    return;
  }

  // Initialise SVG and card layers once per container.
  if (!_liveSvgMap.has(container)) {
    container.innerHTML = '';
    container.style.position = 'relative';
    container.style.overflow = 'auto';

    const svgEl = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svgEl.style.cssText = 'position:absolute;top:0;left:0;pointer-events:none;overflow:visible;';
    container.appendChild(svgEl);
    _liveSvgMap.set(container, window.d3.select(svgEl));

    const cardLayerEl = document.createElement('div');
    cardLayerEl.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;';
    container.appendChild(cardLayerEl);
    _liveCardMap.set(container, window.d3.select(cardLayerEl) as unknown as D3Selection);
  }

  const svg       = _liveSvgMap.get(container)!;
  const cardLayer = _liveCardMap.get(container)!;

  const d3Root = window.d3.hierarchy<LiveOrgNode>(root, d => d.children.length ? d.children : null);

  const treeLayout = window.d3.tree<LiveOrgNode>().nodeSize([NODE_W + NODE_GAP_X, NODE_H + NODE_GAP_Y]);
  (treeLayout as unknown as D3TreeLayout)(d3Root as unknown as D3HierarchyNode);

  let minX = Infinity, maxX = -Infinity, maxY = -Infinity;
  (d3Root as unknown as D3HierarchyNode).each(d => {
    if (d.x < minX) minX = d.x;
    if (d.x > maxX) maxX = d.x;
    if (d.y > maxY) maxY = d.y;
  });

  const treeW   = maxX - minX + NODE_W + 80;
  const treeH   = maxY + NODE_H + 80;
  const canvasW = Math.max(container.clientWidth || 900, treeW);
  const canvasH = Math.max(400, treeH);
  const offsetX = canvasW / 2 - (minX + maxX) / 2;
  const offsetY = 48;

  container.style.minHeight = `${canvasH}px`;
  svg.attr('width', canvasW).attr('height', canvasH);

  const linkGen = window.d3.linkVertical()
    .x((d: D3HierarchyNode) => d.x + offsetX)
    .y((d: D3HierarchyNode) => d.y + offsetY + NODE_H);

  // d.data here is LiveOrgNode (D3 hierarchy wraps LiveOrgNode); the actual
  // RunTreeNodeRow is one level deeper at d.data.data.
  svg.selectAll('.od-link')
    .data((d3Root as unknown as D3HierarchyNode).links() as unknown as D3HierarchyNode[], (d: D3HierarchyNode) => {
      const lnk = d as unknown as D3HierarchyLink;
      const srcId = (lnk.source?.data as unknown as LiveOrgNode | undefined)?.data?.id ?? '';
      const tgtId = (lnk.target?.data as unknown as LiveOrgNode | undefined)?.data?.id ?? '';
      return `${srcId}-${tgtId}`;
    })
    .join('path')
    .attr('class', 'od-link')
    .attr('d', (d: D3HierarchyNode) => linkGen(d as unknown as D3HierarchyLink) ?? '');

  const descendants = (d3Root as unknown as D3HierarchyNode).descendants();
  const cards = cardLayer.selectAll('.od-node')
    .data(descendants, (d: D3HierarchyNode) => (d.data as unknown as LiveOrgNode).data.id);

  const entered = cards.enter().append('div').attr('class', 'od-node od-node--live');
  const all     = entered.merge(cards);

  all
    .style('left', (d: D3HierarchyNode) => `${d.x + offsetX - NODE_W / 2}px`)
    .style('top',  (d: D3HierarchyNode) => `${d.y + offsetY}px`)
    .classed('od-node--coordinator', (d: D3HierarchyNode) => {
      const row = (d.data as unknown as LiveOrgNode).data;
      return !!row.role && isCoordinator(row.role);
    })
    .classed('od-node--worker', (d: D3HierarchyNode) => {
      const row = (d.data as unknown as LiveOrgNode).data;
      return !!row.role && !isCoordinator(row.role);
    })
    .html((d: D3HierarchyNode) => {
      const row = (d.data as unknown as LiveOrgNode).data;
      return liveNodeCardHtml(row, repo, initiative);
    });

  cards.exit().remove();
}

// ── Org preset types + storage ────────────────────────────────────────────────

interface OrgPreset {
  id: string;
  name: string;
  tree: OrgNode;
  updatedAt: string;
}

/** Lightweight template description — no live OrgNode IDs. */
interface PresetTemplate {
  role: string;
  figure?: string;
  children?: PresetTemplate[];
}

/** Clone a preset template into fresh OrgNodes with new unique IDs. */
function buildTree(tmpl: PresetTemplate): OrgNode {
  return {
    ...makeNode(tmpl.role, tmpl.figure ?? ''),
    children: (tmpl.children ?? []).map(c => buildTree(c)),
  };
}

/** Human-readable labels for preset group slugs. */
const GROUP_LABELS: Record<string, string> = {
  engineering: 'Engineering',
  data:        'ML / Data',
  executive:   'Executive',
  product:     'Product',
  marketing:   'Marketing',
  security:    'Security',
  operations:  'Operations',
};

/** Display order for preset groups in the picker. */
const GROUP_ORDER = ['engineering', 'data', 'executive', 'product', 'marketing', 'security', 'operations'];

function presetsKey(repo: string): string {
  return `ac_presets_${repo}`;
}

function loadUserPresets(repo: string): OrgPreset[] {
  try {
    const raw = localStorage.getItem(presetsKey(repo));
    if (!raw) return [];
    return (JSON.parse(raw) as OrgPreset[]).map(p => ({
      ...p,
      tree: restoreNode(p.tree as Partial<OrgNode>),
    }));
  } catch { return []; }
}

function saveUserPresets(repo: string, presets: OrgPreset[]): void {
  try {
    localStorage.setItem(presetsKey(repo), JSON.stringify(presets));
  } catch { /* quota exceeded — non-critical */ }
}

// ── D3 canvas constants ───────────────────────────────────────────────────────

const NODE_W     = 240;
const NODE_H     = 108;
const NODE_GAP_X = 48;
const NODE_GAP_Y = 72;

// WeakMaps for D3 canvas bookkeeping — avoids augmenting HTMLElement directly.
const svgMap   = new WeakMap<HTMLElement, D3SVGSelection>();
const cardMap  = new WeakMap<HTMLElement, D3Selection>();

// ── D3 rendering ──────────────────────────────────────────────────────────────

function renderD3(
  rootData:    OrgNode,
  container:   HTMLElement,
  figures:     FigureItem[],
  selectedId:  string | null,
  repo:        string,
  onAdd:       (id: string) => void,
  onRemove:    (id: string) => void,
  onSelect:    (id: string) => void,
): void {
  // ── Bootstrap canvas on first call ────────────────────────────────────────
  if (!svgMap.has(container)) {
    container.style.position = 'relative';
    container.style.overflow = 'auto';

    const svgEl = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svgEl.style.cssText = 'position:absolute;top:0;left:0;pointer-events:none;overflow:visible;';
    container.appendChild(svgEl);
    svgMap.set(container, window.d3.select(svgEl));

    const cardLayerEl = document.createElement('div');
    cardLayerEl.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;';
    container.appendChild(cardLayerEl);
    const cardSel = window.d3.select(cardLayerEl) as unknown as D3Selection;
    cardMap.set(container, cardSel);
  }

  const svg       = svgMap.get(container)!;
  const cardLayer = cardMap.get(container)!;

  // ── D3 tree layout ────────────────────────────────────────────────────────
  const hierarchy  = window.d3.hierarchy(rootData, d => d.children.length ? d.children : null);
  const treeLayout = window.d3.tree().nodeSize([NODE_W + NODE_GAP_X, NODE_H + NODE_GAP_Y]);
  treeLayout(hierarchy);

  let minX = Infinity, maxX = -Infinity, maxY = -Infinity;
  hierarchy.each(d => {
    if (d.x < minX) minX = d.x;
    if (d.x > maxX) maxX = d.x;
    if (d.y > maxY) maxY = d.y;
  });

  const treeW   = maxX - minX + NODE_W + 80;
  const treeH   = maxY + NODE_H + 80;
  const canvasW = Math.max(container.clientWidth || 900, treeW);
  const canvasH = Math.max(400, treeH);
  const offsetX = canvasW / 2 - (minX + maxX) / 2;
  const offsetY = 48;

  container.style.minHeight = `${canvasH}px`;
  svg.attr('width', canvasW).attr('height', canvasH);

  // ── Bezier edges ──────────────────────────────────────────────────────────
  const linkGen = window.d3.linkVertical()
    .x((d: D3HierarchyNode) => d.x + offsetX)
    .y((d: D3HierarchyNode) => d.y + offsetY + NODE_H);

  svg.selectAll('.od-link')
    .data(hierarchy.links() as unknown as D3HierarchyNode[], (d: D3HierarchyNode) => {
      const lnk = d as unknown as D3HierarchyLink;
      return `${lnk.source?.data?.id}-${lnk.target?.data?.id}`;
    })
    .join('path')
    .attr('class', 'od-link')
    .attr('d', (d: D3HierarchyNode) => linkGen(d as unknown as D3HierarchyLink) ?? '');

  // ── Node cards ────────────────────────────────────────────────────────────
  const figMap = new Map(figures.map(f => [f.id, f.name]));

  const descendants = hierarchy.descendants();
  const cards = cardLayer.selectAll('.od-node')
    .data(descendants, (d: D3HierarchyNode) => d.data.id);

  const entered = cards.enter().append('div').attr('class', 'od-node');
  const all     = entered.merge(cards);

  all
    .style('left', (d: D3HierarchyNode) => `${d.x + offsetX - NODE_W / 2}px`)
    .style('top',  (d: D3HierarchyNode) => `${d.y + offsetY}px`)
    .classed('od-node--selected',    (d: D3HierarchyNode) => d.data.id === selectedId)
    .classed('od-node--coordinator', (d: D3HierarchyNode) => !!d.data.role && isCoordinator(d.data.role))
    .classed('od-node--worker',      (d: D3HierarchyNode) => !!d.data.role && !isCoordinator(d.data.role))
    .classed('od-node--pending',     (d: D3HierarchyNode) => !d.data.role && !d.data.launched)
    .classed('od-node--launched',    (d: D3HierarchyNode) => d.data.launched)
    .html((d: D3HierarchyNode) => nodeCardHtml(d, figMap, repo));

  cards.exit().remove();

  // ── Event delegation ──────────────────────────────────────────────────────
  // Rebind every render — .html() replaces inner DOM, tearing out old listeners.
  const layerEl = cardLayer.node();
  layerEl.querySelectorAll<HTMLButtonElement>('.od-node__btn--add').forEach(btn => {
    btn.addEventListener('click', e => { e.stopPropagation(); onAdd(btn.dataset['id'] ?? ''); });
  });
  layerEl.querySelectorAll<HTMLButtonElement>('.od-node__btn--remove').forEach(btn => {
    btn.addEventListener('click', e => { e.stopPropagation(); onRemove(btn.dataset['id'] ?? ''); });
  });
  layerEl.querySelectorAll<HTMLButtonElement>('.od-node__btn--edit').forEach(btn => {
    btn.addEventListener('click', e => { e.stopPropagation(); onSelect(btn.dataset['id'] ?? ''); });
  });
}

// ── Node card HTML ────────────────────────────────────────────────────────────

function nodeCardHtml(
  d:      D3HierarchyNode,
  figMap: Map<string, string>,
  repo:   string,
): string {
  const node = d.data;

  // Build a scope badge shown on the card (phase label or issue # link).
  function scopeBadge(): string {
    if (node.scope === 'phase' && node.scopeLabel) {
      return `<div class="od-node__scope">⌖ ${node.scopeLabel}</div>`;
    }
    if (node.scope === 'issue' && node.scopeIssueNumber != null) {
      const url = `https://github.com/${repo}/issues/${node.scopeIssueNumber}`;
      return `<a class="od-node__issue-link" href="${url}" target="_blank" rel="noopener noreferrer"
                 title="Open issue #${node.scopeIssueNumber} on GitHub"
                 onclick="event.stopPropagation()"
               ># ${node.scopeIssueNumber}</a>`;
    }
    return '';
  }

  if (node.launched) {
    const removeBtn = d.depth > 0
      ? `<button class="od-node__btn od-node__btn--remove" data-id="${node.id}" title="Remove">×</button>`
      : '';
    return `
      <div class="od-node__tier od-node__tier--launched">✅ launched</div>
      <div class="od-node__role">${roleLabel(node.role)}</div>
      ${scopeBadge()}
      <div class="od-node__run-id">${node.runId}</div>
      <div class="od-node__actions">
        <button class="od-node__btn od-node__btn--edit" data-id="${node.id}" title="View">edit</button>
        ${removeBtn}
      </div>`;
  }

  const configured = !!node.role;
  const tier       = configured ? (isCoordinator(node.role) ? 'coordinator' : 'worker') : 'pending';
  const roleLbl    = configured ? roleLabel(node.role) : '— define role —';
  const figName    = configured
    ? (node.figure ? (figMap.get(node.figure) ?? node.figure) : 'role default')
    : '';
  const removeBtn  = d.depth > 0
    ? `<button class="od-node__btn od-node__btn--remove" data-id="${node.id}" title="Remove">×</button>`
    : '';
  // Only show "+ child" once the node has an identity — the parent role drives
  // child-type filtering, so a pending (roleless) node can't constrain it.
  const addBtn = configured
    ? `<button class="od-node__btn od-node__btn--add" data-id="${node.id}" title="Add child">+ child</button>`
    : '';

  return `
    <div class="od-node__tier od-node__tier--${tier}">${tier}</div>
    <div class="od-node__role${configured ? '' : ' od-node__role--empty'}">${roleLbl}</div>
    ${configured ? `<div class="od-node__figure">${figName}</div>` : ''}
    ${scopeBadge()}
    <div class="od-node__actions">
      ${addBtn}
      <button class="od-node__btn od-node__btn--edit" data-id="${node.id}" title="Edit node">edit</button>
      ${removeBtn}
    </div>`;
}

// ── Alpine magic properties ───────────────────────────────────────────────────

interface AlpineMagics {
  $nextTick(callback?: () => void): Promise<void>;
}

// ── Component interface ───────────────────────────────────────────────────────

interface OrgDesignerComponent {
  // ── Overlay state
  open: boolean;
  initiative: string;
  repo: string;
  figures: FigureItem[];
  /** role slug → compatible figure IDs from role-taxonomy.yaml (injected at page load). */
  roleFigureMap: Record<string, string[]>;

  // ── Preset management
  presetsOpen: boolean;
  presetsLoading: boolean;
  builtInPresets: ApiPresetSummary[];
  userPresets: OrgPreset[];
  activePresetId: string | null;
  saveAsMode: boolean;
  saveAsName: string;

  // ── Node editor
  selectedNodeId: string | null;
  editType: 'coordinator' | 'worker';
  editParentRole: string | null;
  editRole: string;
  editFigure: string;
  editScope: 'full_initiative' | 'phase' | 'issue';
  editScopeLabel: string;
  editScopeIssueNumber: number | null;
  editError: string | null;
  phases: PhaseItem[];
  issues: IssueItem[];

  // ── Submission
  launching: boolean;
  launchError: string | null;
  launchSuccess: boolean;
  launchResult: DispatchResponse | null;
  startAgentLoading: boolean;
  startAgentError: string | null;
  startAgentDone: boolean;

  // ── Live mode (not Alpine-reactive — kept in plain object fields)
  liveMode: boolean;
  liveNodes: RunTreeNodeRow[];
  activeBatches: BatchSummaryRow[];
  liveBatchId: string | null;
  worktreeIndexEnabled: boolean;

  // ── Internal (D3 / mutable tree — not Alpine-reactive)
  _root: OrgNode | null;
  _container: HTMLElement | null;
  _liveEs: EventSource | null;
  _liveContainer: HTMLElement | null;

  // ── Static data exposed to template
  roleGroups: RoleGroup[];

  // ── Computed
  readonly selectedNode: OrgNode | null;
  readonly filteredRoleGroups: RoleGroup[];
  readonly filteredFigures: FigureItem[];
  readonly availableEditTypes: Array<'coordinator' | 'worker'>;
  readonly scopeError: string;
  readonly launchReady: boolean;
  readonly launchPreviewText: string;
  readonly activePresetName: string;
  readonly activePresetIsBuiltIn: boolean;
  readonly groupedBuiltIns: Array<{ group: string; label: string; presets: ApiPresetSummary[] }>;
  /** True when the selected node is the root (scope picker only meaningful there). */
  readonly isRootSelected: boolean;

  // ── Methods
  openDesigner(label: string, repo: string, figures: FigureItem[]): void;
  onRoleChange(): void;
  close(): void;
  loadPreset(id: string): void;
  loadBlank(): void;
  saveCurrentAsPreset(): void;
  updateActivePreset(): void;
  deletePreset(id: string, e: MouseEvent): void;
  _openCanvas(): void;
  addChild(parentId: string): void;
  removeNodeById(id: string): void;
  selectNodeById(id: string): void;
  _openEditor(id: string, parentRole: string | null, type: 'coordinator' | 'worker'): void;
  onTypeChange(): void;
  applyEdit(): void;
  cancelEdit(): void;
  clearDesign(): void;
  _saveToStorage(): void;
  _loadPhases(): Promise<void>;
  _findParentRole(childId: string): string | null;
  _render(): void;
  launch(): Promise<void>;
  startAgent(): Promise<void>;
  // ── Live mode methods
  enterLiveMode(): void;
  enterDesignMode(): void;
  switchBatch(batchId: string): void;
  _connectLiveSSE(batchId: string): void;
  _disconnectLiveSSE(): void;
  _renderLive(): void;
}

// ── Alpine component factory ──────────────────────────────────────────────────

export function orgDesigner(): OrgDesignerComponent {
  return {
    // ── Overlay state ─────────────────────────────────────────────────────────
    open:          false,
    initiative:    '',
    repo:          '',
    figures:       [],
    roleFigureMap: (typeof window !== 'undefined' && '_roleFigureMap' in window
      ? (window as unknown as Record<string, unknown>)['_roleFigureMap'] as Record<string, string[]>
      : {}) as Record<string, string[]>,

    // ── Preset management ─────────────────────────────────────────────────────
    presetsOpen:    true,
    presetsLoading: false,
    builtInPresets: [] as ApiPresetSummary[],
    userPresets:    [] as OrgPreset[],
    activePresetId: null as string | null,
    saveAsMode:     false,
    saveAsName:     '',

    // ── Node editor state ─────────────────────────────────────────────────────
    selectedNodeId: null,
    editType:       'coordinator',
    editParentRole: null,
    editRole:             '',
    editFigure:           '',
    editScope:            'full_initiative',
    editScopeLabel:       '',
    editScopeIssueNumber: null,
    editError:            null,
    phases:               [],
    issues:               [],

    // ── Submission state ──────────────────────────────────────────────────────
    launching:    false,
    launchError:  null,
    launchSuccess:false,
    launchResult: null,
    startAgentLoading: false,
    startAgentError:   null,
    startAgentDone:    false,

    // ── Live mode state ───────────────────────────────────────────────────────
    liveMode:       false,
    liveNodes:      [] as RunTreeNodeRow[],
    activeBatches:  [] as BatchSummaryRow[],
    liveBatchId:    null as string | null,
    worktreeIndexEnabled: (typeof window !== 'undefined' && '_worktreeIndexEnabled' in window
      ? (window as unknown as Record<string, unknown>)['_worktreeIndexEnabled'] as boolean
      : false),

    // ── Internal ──────────────────────────────────────────────────────────────
    _root:          null,
    _container:     null,
    _liveEs:        null as EventSource | null,
    _liveContainer: null as HTMLElement | null,
    roleGroups: ROLE_GROUPS,

    // ── Computed ──────────────────────────────────────────────────────────────

    get selectedNode(): OrgNode | null {
      if (!this.selectedNodeId || !this._root) return null;
      return findNode(this._root, this.selectedNodeId);
    },

    /** Role groups filtered by editType AND parent role constraints. */
    get filteredRoleGroups(): RoleGroup[] {
      return filterGroupsForParent(ROLE_GROUPS, this.editParentRole, this.editType);
    },

    /**
     * Figure list filtered to those compatible with the currently selected role.
     * Falls back to the full list when the role is blank or has no taxonomy entry.
     */
    get filteredFigures(): FigureItem[] {
      if (!this.editRole) return this.figures;
      const compatible = this.roleFigureMap[this.editRole];
      if (!compatible || compatible.length === 0) return this.figures;
      const allowed = new Set(compatible);
      return this.figures.filter(f => allowed.has(f.id));
    },

    /** Which type tabs are valid given the parent — hides irrelevant radio options. */
    get availableEditTypes(): Array<'coordinator' | 'worker'> {
      if (!this.editParentRole) return ['coordinator', 'worker'];
      return availableChildTypes(this.editParentRole);
    },

    /** Non-empty string when the current org tree has a scope configuration
     *  error that must be resolved before launching. */
    get scopeError(): string {
      if (!this._root) return '';
      // If the edit panel is open for the root node and a ticket is already selected
      // in the panel (even before Apply), the user is actively fixing the config — suppress.
      const editingRoot = this.selectedNodeId === this._root.id;
      if (editingRoot && this.editScope === 'issue' && this.editScopeIssueNumber !== null) {
        return '';
      }
      if (this._root.scope === 'issue' && this._root.scopeIssueNumber === null) {
        return 'Select a ticket: open the node editor, choose Ticket scope, and pick a ticket.';
      }
      return '';
    },

    get launchReady(): boolean {
      return !!(
        this._root &&
        this._root.role &&
        !this.scopeError &&
        !this.launching &&
        !this.launchSuccess
      );
    },

    get launchPreviewText(): string {
      if (!this._root || !this._root.role) return 'Configure root node first';
      const figMap  = new Map(this.figures.map(f => [f.id, f.name]));
      const figName = this._root.figure ? (figMap.get(this._root.figure) ?? this._root.figure) : 'role default';
      const extra   = countNodes(this._root) - 1;
      const note    = extra > 0 ? ` + ${extra} child${extra === 1 ? '' : 'ren'}` : '';
      return `Launch ${roleLabel(this._root.role)} (${figName})${note} →`;
    },

    /** True when the root is a lone worker (no coordinator) with full_initiative scope.
     *
     * A standalone worker dispatched against a full initiative won't automatically
     * pick up any tickets — it needs a coordinator above it to survey issues and
     * assign work.  Show a warning but don't block launch (advanced users may
     * know what they're doing).
     */
    get loneWorkerWarning(): string {
      if (!this._root || !this._root.role) return '';
      if (isCoordinator(this._root.role)) return '';
      // When scoped to a specific issue or phase the user has made an explicit choice — no warning.
      if (this._root.scope === 'issue' || this._root.scope === 'phase') return '';
      if (this._root.scope !== 'full_initiative') return '';
      return `⚠️ "${roleLabel(this._root.role)}" is a worker, not a coordinator. Workers don't pick up tickets automatically. Add a coordinator (e.g. CTO) as the root, or set scope to "Phase" or "Ticket".`;
    },

    get isRootSelected(): boolean {
      return !!(this._root && this.selectedNodeId === this._root.id);
    },

    get activePresetName(): string {
      if (!this.activePresetId) return '';
      const builtin = this.builtInPresets.find(t => t.id === this.activePresetId);
      if (builtin) return builtin.name;
      return this.userPresets.find(p => p.id === this.activePresetId)?.name ?? '';
    },

    get activePresetIsBuiltIn(): boolean {
      return this.builtInPresets.some(t => t.id === this.activePresetId);
    },

    get groupedBuiltIns(): Array<{ group: string; label: string; presets: ApiPresetSummary[] }> {
      const map = new Map<string, ApiPresetSummary[]>();
      for (const p of this.builtInPresets) {
        const list = map.get(p.group) ?? [];
        list.push(p);
        map.set(p.group, list);
      }
      return GROUP_ORDER
        .filter(g => map.has(g))
        .map(g => ({ group: g, label: GROUP_LABELS[g] ?? g, presets: map.get(g)! }));
    },

    // ── Lifecycle ──────────────────────────────────────────────────────────────

    openDesigner(label: string, repo: string, figures: FigureItem[]): void {
      this.initiative     = label;
      this.repo           = repo;
      this.figures        = figures;
      this.launchError    = null;
      this.launchSuccess  = false;
      this.launchResult   = null;
      this.launching      = false;
      this.startAgentLoading = false;
      this.startAgentError   = null;
      this.startAgentDone    = false;
      this.selectedNodeId = null;
      this.saveAsMode     = false;
      this.saveAsName     = '';
      this.userPresets    = loadUserPresets(repo);
      this.open           = true;

      void this._loadPhases();

      // Fetch built-in preset summaries from the API.
      this.presetsLoading = true;
      void fetch('/api/org-presets')
        .then(async r => {
          if (r.ok) this.builtInPresets = await r.json() as ApiPresetSummary[];
        })
        .catch(() => { /* non-critical — grid will be empty */ })
        .finally(() => { this.presetsLoading = false; });

      // Check for active batches — if any exist, default to Live mode.
      void (async () => {
        try {
          const r = await fetch(
            `/api/org/batches/${encodeURIComponent(label)}`
          );
          if (r.ok) {
            this.activeBatches = await r.json() as BatchSummaryRow[];
          }
        } catch {
          // Non-critical — live mode just won't activate automatically.
        }

        if (this.activeBatches.length > 0) {
          // There are known dispatch batches — enter Live mode immediately.
          const newest = this.activeBatches[0];
          this.presetsOpen = false;
          this.enterLiveMode();
          this._connectLiveSSE(newest.batch_id);
        } else {
          // No active batches — restore design canvas or show preset picker.
          const saved = loadFromStorage(repo, label);
          if (saved) {
            this._root       = saved;
            this.presetsOpen = false;
            this._openCanvas();
          } else {
            this._root          = null;
            this.activePresetId = null;
            this.presetsOpen    = true;
          }
        }
      })();
    },

    close(): void {
      this._disconnectLiveSSE();
      this.open = false;
    },

    // ── Preset management ─────────────────────────────────────────────────────

    loadPreset(id: string): void {
      if (id.startsWith('builtin-')) {
        // Fetch the tree template from the API, then open the canvas.
        void (async () => {
          const resp = await fetch(`/api/org-presets/${id}`);
          if (!resp.ok) return;
          const detail = await resp.json() as ApiPresetDetail;
          this._root          = buildTree(detail.template);
          this.activePresetId = id;
          this.presetsOpen    = false;
          this.selectedNodeId = null;
          this._openCanvas();
        })();
        return;
      }
      // User-saved preset — already in memory.
      const user = this.userPresets.find(p => p.id === id);
      if (!user) return;
      // Clone via serialize/deserialize so edits don't mutate the stored preset.
      this._root = restoreNode(JSON.parse(JSON.stringify(user.tree)) as Partial<OrgNode>);
      this.activePresetId = id;
      this.presetsOpen    = false;
      this.selectedNodeId = null;
      this._saveToStorage();
      this._openCanvas();
    },

    loadBlank(): void {
      this._root          = makeNode('', '');
      this.activePresetId = null;
      this.presetsOpen    = false;
      this.selectedNodeId = null;
      clearStorage(this.repo, this.initiative);
      this._openCanvas();
    },

    saveCurrentAsPreset(): void {
      if (!this.saveAsName.trim() || !this._root) return;
      const preset: OrgPreset = {
        id:        `user_${Date.now()}`,
        name:      this.saveAsName.trim(),
        tree:      this._root,
        updatedAt: new Date().toISOString(),
      };
      this.userPresets    = [...this.userPresets, preset];
      saveUserPresets(this.repo, this.userPresets);
      this.activePresetId = preset.id;
      this.saveAsMode     = false;
      this.saveAsName     = '';
    },

    updateActivePreset(): void {
      if (!this.activePresetId || !this._root || this.activePresetIsBuiltIn) return;
      const idx = this.userPresets.findIndex(p => p.id === this.activePresetId);
      if (idx === -1) return;
      const updated: OrgPreset = {
        ...this.userPresets[idx],
        tree:      this._root,
        updatedAt: new Date().toISOString(),
      };
      const next = [...this.userPresets];
      next[idx]       = updated;
      this.userPresets = next;
      saveUserPresets(this.repo, this.userPresets);
    },

    deletePreset(id: string, e: MouseEvent): void {
      e.stopPropagation();
      this.userPresets = this.userPresets.filter(p => p.id !== id);
      saveUserPresets(this.repo, this.userPresets);
      if (this.activePresetId === id) this.activePresetId = null;
    },

    _openCanvas(): void {
      void (this as unknown as AlpineMagics).$nextTick(() => {
        this._container = document.getElementById('od-canvas');
        requestAnimationFrame(() => {
          this._render();
          if (this._root && !this._root.role) {
            this._openEditor(this._root.id, null, 'coordinator');
          }
        });
      });
    },

    // ── Phase loading ─────────────────────────────────────────────────────────

    async _loadPhases(): Promise<void> {
      try {
        const url = `/api/dispatch/context?label=${encodeURIComponent(this.initiative)}&repo=${encodeURIComponent(this.repo)}`;
        const res = await fetch(url);
        if (res.ok) {
          const data = await res.json() as ContextResponse;
          this.phases = data.phases ?? [];
          this.issues = data.issues ?? [];
        }
      } catch {
        // Non-critical — scope picker just won't show phases or issues.
      }
    },

    // ── Tree mutations ────────────────────────────────────────────────────────

    addChild(parentId: string): void {
      if (!this._root) return;
      const parent = findNode(this._root, parentId);
      if (!parent) return;
      const child = makeNode('', '');
      parent.children.push(child);
      this._render();
      this._saveToStorage();
      // Prefer coordinator children for coordinator parents, worker otherwise.
      const types = availableChildTypes(parent.role);
      const defaultType: 'coordinator' | 'worker' =
        isCoordinator(parent.role) && types.includes('coordinator') ? 'coordinator' : 'worker';
      this._openEditor(child.id, parent.role, defaultType);
    },

    removeNodeById(id: string): void {
      if (!this._root || id === this._root.id) return;
      pruneNode(this._root, id);
      if (this.selectedNodeId === id) this.selectedNodeId = null;
      this._render();
      this._saveToStorage();
    },

    selectNodeById(id: string): void {
      if (!this._root) return;
      const node = findNode(this._root, id);
      if (!node) return;
      const parentRole = this._findParentRole(id);
      const type: 'coordinator' | 'worker' = node.role
        ? (isCoordinator(node.role) ? 'coordinator' : 'worker')
        : 'coordinator';
      this._openEditor(id, parentRole, type);
    },

    /** Walk the tree to find the role of the parent of *childId*. */
    _findParentRole(childId: string): string | null {
      function walk(node: OrgNode, target: string): string | null {
        for (const c of node.children) {
          if (c.id === target) return node.role || null;
          const found = walk(c, target);
          if (found !== undefined) return found;
        }
        return undefined as unknown as null;
      }
      if (!this._root) return null;
      return walk(this._root, childId);
    },

    _openEditor(id: string, parentRole: string | null, type: 'coordinator' | 'worker'): void {
      if (!this._root) return;
      const node = findNode(this._root, id);
      if (!node) return;
      this.selectedNodeId       = id;
      this.editParentRole       = parentRole;
      this.editType             = type;
      this.editRole             = node.role;
      this.editFigure           = node.figure;
      this.editScope            = node.scope;
      this.editScopeLabel       = node.scopeLabel;
      this.editScopeIssueNumber = node.scopeIssueNumber;
    },

    onTypeChange(): void {
      this.editRole = '';
      this.editFigure = '';
    },

    /**
     * Called when the role select changes.
     * Clears editFigure when the current selection is no longer in the
     * filtered figure list for the new role, preventing a stale value
     * from being sent to the backend.
     */
    onRoleChange(): void {
      if (!this.editFigure) return;
      const compatible = this.roleFigureMap[this.editRole];
      if (compatible && compatible.length > 0 && !compatible.includes(this.editFigure)) {
        this.editFigure = '';
      }
    },

    applyEdit(): void {
      this.editError = null;
      if (!this._root) return;
      const node = findNode(this._root, this.selectedNodeId ?? '');
      if (!node) return;

      if (this.editScope === 'issue' && this.editScopeIssueNumber === null) {
        this.editError = 'Select a ticket before applying.';
        return;
      }

      node.role             = this.editRole;
      node.figure           = this.editFigure;
      node.scope            = this.editScope;
      node.scopeLabel       = this.editScope === 'phase' ? this.editScopeLabel : '';
      node.scopeIssueNumber = this.editScope === 'issue' ? this.editScopeIssueNumber : null;
      this.selectedNodeId = null;
      this._render();
      this._saveToStorage();
    },

    cancelEdit(): void {
      if (this.selectedNodeId && this._root) {
        const node = findNode(this._root, this.selectedNodeId);
        if (node && !node.role) {
          if (node.id === this._root.id) {
            // Blank root cancelled — go back to preset picker rather than closing.
            this.presetsOpen    = true;
            this.selectedNodeId = null;
            return;
          }
          // Blank child cancelled — remove it.
          pruneNode(this._root, this.selectedNodeId);
          this._render();
          this._saveToStorage();
        }
      }
      this.selectedNodeId = null;
    },

    /** Clear the current canvas and return to the preset picker. */
    clearDesign(): void {
      clearStorage(this.repo, this.initiative);
      this._root          = null;
      this.selectedNodeId = null;
      this.activePresetId = null;
      this.launchSuccess  = false;
      this.launchResult   = null;
      this.launchError    = null;
      this.saveAsMode     = false;
      this.saveAsName     = '';
      this.presetsOpen    = true;
    },

    // ── localStorage ──────────────────────────────────────────────────────────

    _saveToStorage(): void {
      if (this._root) saveToStorage(this.repo, this.initiative, this._root);
    },

    // ── D3 render ─────────────────────────────────────────────────────────────

    _render(): void {
      if (!this._container || !this._root) return;
      renderD3(
        this._root,
        this._container,
        this.figures,
        this.selectedNodeId,
        this.repo,
        id => { this.addChild(id); },
        id => { this.removeNodeById(id); },
        id => { this.selectNodeById(id); },
      );
    },

    // ── Launch ────────────────────────────────────────────────────────────────

    async launch(): Promise<void> {
      if (!this.launchReady || !this._root) return;
      this.launching  = true;
      this.launchError = null;

      const payload = {
        label:                   this.initiative,
        scope:                   this._root.scope,
        scope_label:             this._root.scope === 'phase' ? this._root.scopeLabel : undefined,
        scope_issue_number:      this._root.scope === 'issue' ? this._root.scopeIssueNumber : undefined,
        repo:                    this.repo,
        role:                    this._root.role,
        cognitive_arch_override: this._root.figure || null,
        org_tree:                serializeNode(this._root),
      };

      try {
        const res  = await fetch('/api/dispatch/label', {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify(payload),
        });
        const data = await res.json() as DispatchResponse | DispatchError;
        if (!res.ok) {
          const errData = data as DispatchError;
          const detail = errData.detail;
          if (Array.isArray(detail)) {
            // FastAPI 422 validation errors — format as readable list.
            this.launchError = detail.map(e => e.msg).join('; ') || `Error ${res.status}`;
          } else {
            this.launchError = detail ?? `Error ${res.status}`;
          }
        } else {
          const dispatched    = data as DispatchResponse;
          this.launchResult   = dispatched;
          this.launchSuccess  = true;
          // Mark root as launched — D3 re-renders it green with run_id.
          this._root.launched = true;
          this._root.runId    = dispatched.run_id;
          this._render();
          // Immediately start the agent loop — no separate button needed.
          await this.startAgent();
        }
      } catch (err) {
        this.launchError = `Network error: ${err instanceof Error ? err.message : String(err)}`;
      } finally {
        this.launching = false;
      }
    },

    /** Start the agent loop for a run in pending_launch state (POST /api/runs/{run_id}/execute).
     *
     * Called automatically by launch() after a successful dispatch.  Also available
     * directly from the inspector panel for runs stuck in pending_launch.
     */
    async startAgent(): Promise<void> {
      if (!this.launchResult?.run_id || this.startAgentLoading) return;
      this.startAgentLoading = true;
      this.startAgentError   = null;
      try {
        const res = await fetch(`/api/runs/${encodeURIComponent(this.launchResult.run_id)}/execute`, {
          method: 'POST',
        });
        if (!res.ok) {
          const text = await res.text();
          this.startAgentError = text.slice(0, 200) || `Error ${res.status}`;
          return;
        }
        this.startAgentDone = true;
      } catch (err) {
        this.startAgentError = err instanceof Error ? err.message : String(err);
      } finally {
        this.startAgentLoading = false;
      }
    },

    // ── Live mode ─────────────────────────────────────────────────────────────

    /** Switch to Live mode (real-time agent tree). */
    enterLiveMode(): void {
      this.liveMode = true;
      void (this as unknown as AlpineMagics).$nextTick(() => {
        this._liveContainer = document.getElementById('od-live-canvas');
        if (this._liveContainer && this.liveNodes.length) {
          this._renderLive();
        }
      });
    },

    /** Switch back to Design mode (editable org tree). */
    enterDesignMode(): void {
      this._disconnectLiveSSE();
      this.liveMode = false;
      // Re-open design canvas if we have a root, otherwise show preset picker.
      void (this as unknown as AlpineMagics).$nextTick(() => {
        if (this._root) {
          this._openCanvas();
        } else {
          const saved = loadFromStorage(this.repo, this.initiative);
          if (saved) {
            this._root       = saved;
            this.presetsOpen = false;
            this._openCanvas();
          } else {
            this.presetsOpen = true;
          }
        }
      });
    },

    /** Switch the live view to a different historical batch. */
    switchBatch(batchId: string): void {
      this.liveBatchId = batchId;
      this._disconnectLiveSSE();
      this._connectLiveSSE(batchId);
    },

    /** Open an EventSource for the live agent tree SSE stream. */
    _connectLiveSSE(batchId: string): void {
      this._disconnectLiveSSE();
      this.liveBatchId = batchId;
      const url = `/api/org/live/${encodeURIComponent(this.initiative)}?batch_id=${encodeURIComponent(batchId)}`;
      const es = new EventSource(url);
      this._liveEs = es;

      es.onmessage = (ev: MessageEvent<string>) => {
        let parsed: LiveSseEvent;
        try {
          parsed = JSON.parse(ev.data) as LiveSseEvent;
        } catch {
          return;
        }
        if (parsed.t === 'tree') {
          this.liveNodes = parsed.nodes;
          this._renderLive();
        }
        // idle and ping: no-op in UI
      };

      es.onerror = () => {
        // EventSource auto-reconnects on transient failures; no action needed.
      };
    },

    /** Close the live SSE connection if open. */
    _disconnectLiveSSE(): void {
      if (this._liveEs) {
        this._liveEs.close();
        this._liveEs = null;
      }
    },

    /** Re-render the live D3 canvas from the latest liveNodes. */
    _renderLive(): void {
      if (!this.liveMode) return;
      // Ensure the container is resolved each time (Alpine may recreate it).
      const el = document.getElementById('od-live-canvas');
      if (el) this._liveContainer = el;
      if (!this._liveContainer) return;
      renderLiveD3(this.liveNodes, this._liveContainer, this.repo, this.initiative);
    },
  } as OrgDesignerComponent;
}
