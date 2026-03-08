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
 * • Full org tree serialised into .agent-task at launch time
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
  tree(): D3TreeLayout;
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
  scope: 'full_initiative' | 'phase';
  /** Phase sub-label when scope === 'phase'. */
  scopeLabel: string;
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
  scope: 'full_initiative' | 'phase';
  scope_label: string;
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
}

interface ContextResponse {
  phases: PhaseItem[];
  issues: Array<{ number: number; title: string }>;
}

interface DispatchResponse {
  run_id: string;
  batch_id: string;
  tier: string;
  role: string;
  label: string;
  worktree: string;
  host_worktree: string;
  agent_task_path: string;
  status: string;
}

interface DispatchError {
  detail?: string;
}

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
  return { id: `n${++_nodeCounter}`, role, figure, scope: 'full_initiative', scopeLabel: '', launched: false, runId: '', children: [] };
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
    id:          node.id,
    role:        node.role,
    figure:      node.figure,
    scope:       node.scope,
    scope_label: node.scopeLabel,
    children:    node.children.map(serializeNode),
  };
}

/** Restore a node from localStorage JSON, resetting launch state. */
function restoreNode(raw: Partial<OrgNode>): OrgNode {
  const id = raw.id ?? `n${++_nodeCounter}`;
  const num = parseInt(id.replace('n', ''), 10);
  if (!isNaN(num) && num >= _nodeCounter) _nodeCounter = num + 1;
  return {
    id,
    role:       raw.role ?? '',
    figure:     raw.figure ?? '',
    scope:      raw.scope ?? 'full_initiative',
    scopeLabel: raw.scopeLabel ?? '',
    launched:   false,   // always reset on restore — dispatches don't survive refresh
    runId:      '',
    children:   (raw.children ?? []).map(c => restoreNode(c as Partial<OrgNode>)),
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
    .html((d: D3HierarchyNode) => nodeCardHtml(d, figMap, offsetX, offsetY));

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
  _ox:    number,
  _oy:    number,
): string {
  const node = d.data;

  if (node.launched) {
    const removeBtn = d.depth > 0
      ? `<button class="od-node__btn od-node__btn--remove" data-id="${node.id}" title="Remove">×</button>`
      : '';
    return `
      <div class="od-node__tier od-node__tier--launched">✅ launched</div>
      <div class="od-node__role">${roleLabel(node.role)}</div>
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
  const scopeNote  = node.scope === 'phase' && node.scopeLabel
    ? `<div class="od-node__scope">⌖ ${node.scopeLabel}</div>`
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
    ${scopeNote}
    <div class="od-node__actions">
      ${addBtn}
      <button class="od-node__btn od-node__btn--edit" data-id="${node.id}" title="Edit node">edit</button>
      ${removeBtn}
    </div>`;
}

// ── HTML escape helper ────────────────────────────────────────────────────────

function escHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// ── OrgDesigner plain JS class ────────────────────────────────────────────────

/**
 * OrgDesigner — replaces the former Alpine component factory.
 *
 * The overlay HTML is always present in the DOM (hidden via CSS by default).
 * The class owns all interactivity: it attaches event listeners once in the
 * constructor, then calls _syncDOM() and targeted _render*() helpers to keep
 * the DOM in sync with private state.
 *
 * The CustomEvent bridge (`open-org-designer`) from the buildPage Alpine
 * component is unchanged — this class listens on `window` for that event.
 */
class OrgDesigner {
  // ── Overlay / session ──────────────────────────────────────────────────────
  private _initiative  = '';
  private _repo        = '';
  private _figures: FigureItem[]               = [];
  private _roleFigureMap: Record<string, string[]> = {};

  // ── Preset management ──────────────────────────────────────────────────────
  private _presetsOpen    = true;
  private _presetsLoading = false;
  private _builtInPresets: ApiPresetSummary[] = [];
  private _userPresets:    OrgPreset[]         = [];
  private _activePresetId: string | null       = null;
  private _saveAsMode = false;
  private _saveAsName = '';

  // ── Node editor ────────────────────────────────────────────────────────────
  private _selectedNodeId: string | null          = null;
  private _editType:       'coordinator' | 'worker' = 'coordinator';
  private _editParentRole: string | null          = null;
  private _editRole        = '';
  private _editFigure      = '';
  private _editScope:      'full_initiative' | 'phase' = 'full_initiative';
  private _editScopeLabel  = '';
  private _phases:         PhaseItem[]            = [];

  // ── Submission ─────────────────────────────────────────────────────────────
  private _launching     = false;
  private _launchError:  string | null          = null;
  private _launchSuccess = false;
  private _launchResult: DispatchResponse | null = null;

  // ── Internal ───────────────────────────────────────────────────────────────
  private _root:      OrgNode | null      = null;
  private _container: HTMLElement | null  = null;
  private _el:        HTMLElement;

  constructor(el: HTMLElement) {
    this._el = el;
    const win = window as unknown as Record<string, unknown>;
    this._roleFigureMap = (win['_roleFigureMap'] as Record<string, string[]>) ?? {};
    this._attachListeners();
  }

  // ── Typed querySelector helpers ────────────────────────────────────────────

  private _q<T extends Element>(sel: string): T {
    const found = this._el.querySelector<T>(sel);
    if (!found) throw new Error(`OrgDesigner: missing element ${sel}`);
    return found;
  }

  private _qs<T extends Element>(sel: string): T | null {
    return this._el.querySelector<T>(sel);
  }

  // ── Event listener wiring ──────────────────────────────────────────────────

  private _attachListeners(): void {
    // Open: fired by the "Design org →" button inside the buildPage component.
    window.addEventListener('open-org-designer', (e: Event) => {
      const d = (e as CustomEvent<{ label: string; repo: string; figures: FigureItem[] }>).detail;
      this.openDesigner(d.label, d.repo, d.figures);
    });

    // Close on Escape key.
    window.addEventListener('keydown', (e: KeyboardEvent) => {
      if (e.key === 'Escape' && this._el.classList.contains('is-open')) this.close();
    });

    this._q<HTMLButtonElement>('#od-close-btn')
      .addEventListener('click', () => this.close());

    this._q<HTMLButtonElement>('#od-launch-btn')
      .addEventListener('click', () => void this.launch());

    this._q<HTMLButtonElement>('#od-presets-btn')
      .addEventListener('click', () => this.clearDesign());

    this._q<HTMLButtonElement>('#od-blank-btn')
      .addEventListener('click', () => this.loadBlank());

    this._q<HTMLButtonElement>('#od-saveas-btn')
      .addEventListener('click', () => {
        this._saveAsMode = true;
        this._syncDOM();
        queueMicrotask(() => this._qs<HTMLInputElement>('#od-saveas-input')?.focus());
      });

    this._q<HTMLButtonElement>('#od-update-btn')
      .addEventListener('click', () => this.updateActivePreset());

    this._q<HTMLButtonElement>('#od-saveas-confirm')
      .addEventListener('click', () => this.saveCurrentAsPreset());

    this._q<HTMLButtonElement>('#od-saveas-cancel')
      .addEventListener('click', () => {
        this._saveAsMode = false;
        this._syncDOM();
      });

    const saveAsInput = this._q<HTMLInputElement>('#od-saveas-input');
    saveAsInput.addEventListener('input', () => {
      this._saveAsName = saveAsInput.value;
      this._q<HTMLButtonElement>('#od-saveas-confirm').disabled = !this._saveAsName.trim();
    });
    saveAsInput.addEventListener('keydown', (e: KeyboardEvent) => {
      if (e.key === 'Enter')  this.saveCurrentAsPreset();
      if (e.key === 'Escape') { this._saveAsMode = false; this._syncDOM(); }
    });

    // Type radio buttons.
    const radCoord  = this._q<HTMLInputElement>('#od-type-coordinator');
    const radWorker = this._q<HTMLInputElement>('#od-type-worker');
    radCoord.addEventListener('change', () => {
      if (radCoord.checked) { this._editType = 'coordinator'; this._onTypeChange(); }
    });
    radWorker.addEventListener('change', () => {
      if (radWorker.checked) { this._editType = 'worker'; this._onTypeChange(); }
    });

    // Role select — filter figures when role changes.
    const roleSelect = this._q<HTMLSelectElement>('#od-role-select');
    roleSelect.addEventListener('change', () => {
      this._editRole = roleSelect.value;
      this._onRoleChange();
      this._renderFigureOptions();
      this._syncDOM();
    });

    // Figure select — track value.
    const figureSelect = this._q<HTMLSelectElement>('#od-figure-select');
    figureSelect.addEventListener('change', () => { this._editFigure = figureSelect.value; });

    // Editor buttons.
    this._q<HTMLButtonElement>('#od-apply-btn')
      .addEventListener('click', () => this.applyEdit());
    this._q<HTMLButtonElement>('#od-editor-close')
      .addEventListener('click', () => this.cancelEdit());
    this._q<HTMLButtonElement>('#od-cancel-btn')
      .addEventListener('click', () => this.cancelEdit());
  }

  // ── Computed helpers ───────────────────────────────────────────────────────

  private _selectedNode(): OrgNode | null {
    if (!this._selectedNodeId || !this._root) return null;
    return findNode(this._root, this._selectedNodeId);
  }

  private _filteredRoleGroups(): RoleGroup[] {
    return filterGroupsForParent(ROLE_GROUPS, this._editParentRole, this._editType);
  }

  private _filteredFigures(): FigureItem[] {
    if (!this._editRole) return this._figures;
    const compatible = this._roleFigureMap[this._editRole];
    if (!compatible || compatible.length === 0) return this._figures;
    const allowed = new Set(compatible);
    return this._figures.filter(f => allowed.has(f.id));
  }

  private _launchReady(): boolean {
    return !!(this._root && this._root.role && !this._launching && !this._launchSuccess);
  }

  private _launchPreviewText(): string {
    if (!this._root || !this._root.role) return 'Configure root node first';
    const figMap  = new Map(this._figures.map(f => [f.id, f.name]));
    const figName = this._root.figure
      ? (figMap.get(this._root.figure) ?? this._root.figure)
      : 'role default';
    const extra = countNodes(this._root) - 1;
    const note  = extra > 0 ? ` + ${extra} child${extra === 1 ? '' : 'ren'}` : '';
    return `Launch ${roleLabel(this._root.role)} (${figName})${note} →`;
  }

  private _activePresetName(): string {
    if (!this._activePresetId) return '';
    const builtin = this._builtInPresets.find(t => t.id === this._activePresetId);
    if (builtin) return builtin.name;
    return this._userPresets.find(p => p.id === this._activePresetId)?.name ?? '';
  }

  private _activePresetIsBuiltIn(): boolean {
    return this._builtInPresets.some(t => t.id === this._activePresetId);
  }

  private _groupedBuiltIns(): Array<{ group: string; label: string; presets: ApiPresetSummary[] }> {
    const map = new Map<string, ApiPresetSummary[]>();
    for (const p of this._builtInPresets) {
      const list = map.get(p.group) ?? [];
      list.push(p);
      map.set(p.group, list);
    }
    return GROUP_ORDER
      .filter(g => map.has(g))
      .map(g => ({ group: g, label: GROUP_LABELS[g] ?? g, presets: map.get(g)! }));
  }

  // ── DOM sync ───────────────────────────────────────────────────────────────

  /**
   * Synchronise all header, section-visibility, and editor-state DOM
   * with the current private state.  Call after any state mutation.
   */
  private _syncDOM(): void {
    // Initiative text.
    const initEl = this._qs<HTMLElement>('#od-initiative');
    if (initEl) initEl.textContent = this._initiative;

    // Preset badge — canvas view + active preset only.
    const badgeEl = this._qs<HTMLElement>('#od-preset-badge');
    if (badgeEl) {
      const show = !this._presetsOpen && !!this._activePresetId;
      badgeEl.classList.toggle('od-hidden', !show);
      if (show) badgeEl.textContent = this._activePresetName();
    }

    // Save-as row — canvas view + saveAsMode.
    const saveAsRow = this._qs<HTMLElement>('#od-saveas-row');
    if (saveAsRow) {
      const show = !this._presetsOpen && this._saveAsMode;
      saveAsRow.classList.toggle('od-hidden', !show);
    }

    // Canvas controls (update + save-as buttons) — canvas view, not save-as mode.
    const canvasCtrl = this._qs<HTMLElement>('#od-canvas-controls');
    if (canvasCtrl) canvasCtrl.classList.toggle('od-hidden', this._presetsOpen || this._saveAsMode);

    // Update button — only for user (non-builtin) presets.
    const updateBtn = this._qs<HTMLButtonElement>('#od-update-btn');
    if (updateBtn) {
      updateBtn.classList.toggle('od-hidden', !this._activePresetId || this._activePresetIsBuiltIn());
    }

    // Launch error.
    const errEl = this._qs<HTMLElement>('#od-launch-err');
    if (errEl) {
      errEl.classList.toggle('od-hidden', !this._launchError);
      errEl.textContent = this._launchError ?? '';
    }

    // Launch success.
    const okEl     = this._qs<HTMLElement>('#od-launch-ok');
    const runIdEl  = this._qs<HTMLElement>('#od-run-id');
    if (okEl) {
      const showOk = this._launchSuccess && !!this._launchResult;
      okEl.classList.toggle('od-hidden', !showOk);
      if (runIdEl && this._launchResult) runIdEl.textContent = this._launchResult.run_id;
    }

    // Back-to-presets button — canvas view only.
    const presetsBtn = this._qs<HTMLButtonElement>('#od-presets-btn');
    if (presetsBtn) presetsBtn.classList.toggle('od-hidden', this._presetsOpen);

    // Launch button — canvas view only.
    const launchBtn = this._qs<HTMLButtonElement>('#od-launch-btn');
    if (launchBtn) {
      launchBtn.classList.toggle('od-hidden', this._presetsOpen);
      launchBtn.disabled    = !this._launchReady();
      launchBtn.textContent = this._launching ? 'Queuing…' : this._launchPreviewText();
    }

    // Preset picker section vs canvas/body section.
    this._qs<HTMLElement>('#od-presets-section')
      ?.classList.toggle('od-hidden', !this._presetsOpen);
    this._qs<HTMLElement>('#od-body')
      ?.classList.toggle('od-hidden', this._presetsOpen);

    // Node editor panel.
    const editorEl = this._qs<HTMLElement>('#od-editor');
    if (editorEl) {
      const node = this._selectedNode();
      editorEl.classList.toggle('od-hidden', !node);
      if (node) {
        // Title.
        const titleEl = this._qs<HTMLElement>('#od-editor-title');
        if (titleEl) titleEl.textContent = node.role ? 'Edit node' : 'Define node';

        // Type radios + active label.
        const radCoord  = this._qs<HTMLInputElement>('#od-type-coordinator');
        const radWorker = this._qs<HTMLInputElement>('#od-type-worker');
        const lblCoord  = this._qs<HTMLLabelElement>('#od-type-label-coordinator');
        const lblWorker = this._qs<HTMLLabelElement>('#od-type-label-worker');
        if (radCoord)  radCoord.checked  = this._editType === 'coordinator';
        if (radWorker) radWorker.checked = this._editType === 'worker';
        if (lblCoord)  lblCoord.classList.toggle('od-type-opt--active',  this._editType === 'coordinator');
        if (lblWorker) lblWorker.classList.toggle('od-type-opt--active', this._editType === 'worker');

        // Role and figure select current values.
        const roleSelect   = this._qs<HTMLSelectElement>('#od-role-select');
        const figureSelect = this._qs<HTMLSelectElement>('#od-figure-select');
        if (roleSelect)   roleSelect.value   = this._editRole;
        if (figureSelect) figureSelect.value = this._editFigure;

        // Apply button disabled until a role is chosen.
        const applyBtn = this._qs<HTMLButtonElement>('#od-apply-btn');
        if (applyBtn) applyBtn.disabled = !this._editRole;
      }
    }
  }

  // ── Dynamic render helpers ─────────────────────────────────────────────────

  /** Rebuild built-in preset card grid and user presets list. */
  private _renderPresets(): void {
    const loadingEl = this._qs<HTMLElement>('#od-presets-loading');
    if (loadingEl) loadingEl.classList.toggle('od-hidden', !this._presetsLoading);

    const builtinEl = this._qs<HTMLElement>('#od-builtin-sections');
    if (builtinEl) {
      if (this._presetsLoading) {
        builtinEl.innerHTML = '';
      } else {
        builtinEl.innerHTML = this._groupedBuiltIns().map(section => `
          <div class="od-presets__section">
            <h3 class="od-presets__section-title">${escHtml(section.label)}</h3>
            <div class="od-presets__grid">
              ${section.presets.map(t => `
                <button class="od-preset-card od-preset-card--${escHtml(t.accent)}"
                        data-preset-id="${escHtml(t.id)}">
                  <span class="od-preset-card__icon">${escHtml(t.icon)}</span>
                  <span class="od-preset-card__name">${escHtml(t.name)}</span>
                  <span class="od-preset-card__desc">${escHtml(t.description)}</span>
                </button>
              `).join('')}
            </div>
          </div>
        `).join('');
        builtinEl.querySelectorAll<HTMLButtonElement>('[data-preset-id]').forEach(btn => {
          btn.addEventListener('click', () => this.loadPreset(btn.dataset['presetId'] ?? ''));
        });
      }
    }

    const userSection = this._qs<HTMLElement>('#od-user-section');
    const userGrid    = this._qs<HTMLElement>('#od-user-grid');
    if (userSection && userGrid) {
      userSection.classList.toggle('od-hidden', this._userPresets.length === 0);
      userGrid.innerHTML = this._userPresets.map(p => `
        <button class="od-preset-card od-preset-card--user" data-user-preset-id="${escHtml(p.id)}">
          <span class="od-preset-card__icon">✎</span>
          <span class="od-preset-card__date">${escHtml(new Date(p.updatedAt).toLocaleDateString())}</span>
          <span class="od-preset-card__name">${escHtml(p.name)}</span>
          <button class="od-preset-card__delete" data-delete-id="${escHtml(p.id)}"
                  title="Delete this preset">×</button>
        </button>
      `).join('');
      userGrid.querySelectorAll<HTMLButtonElement>('[data-user-preset-id]').forEach(btn => {
        btn.addEventListener('click', () => this.loadPreset(btn.dataset['userPresetId'] ?? ''));
      });
      userGrid.querySelectorAll<HTMLButtonElement>('[data-delete-id]').forEach(btn => {
        btn.addEventListener('click', (e: MouseEvent) => {
          e.stopPropagation();
          this.deletePreset(btn.dataset['deleteId'] ?? '', e);
        });
      });
    }
  }

  /** Rebuild role <optgroup>/<option> elements in the role select. */
  private _renderRoleOptions(): void {
    const select = this._qs<HTMLSelectElement>('#od-role-select');
    if (!select) return;
    select.innerHTML = '<option value="">— select role —</option>' +
      this._filteredRoleGroups().map(group => `
        <optgroup label="${escHtml(group.label)}">
          ${group.roles.map(r =>
            `<option value="${escHtml(r.slug)}">${escHtml(r.label)}</option>`,
          ).join('')}
        </optgroup>
      `).join('');
    select.value = this._editRole;
  }

  /** Rebuild figure <option> elements, filtered by the current role. */
  private _renderFigureOptions(): void {
    const select = this._qs<HTMLSelectElement>('#od-figure-select');
    if (!select) return;
    select.innerHTML = '<option value="">— role default —</option>' +
      this._filteredFigures()
        .map(f => `<option value="${escHtml(f.id)}">${escHtml(f.name)}</option>`)
        .join('');
    select.value = this._editFigure;
  }

  // ── Lifecycle ──────────────────────────────────────────────────────────────

  openDesigner(label: string, repo: string, figures: FigureItem[]): void {
    this._initiative     = label;
    this._repo           = repo;
    this._figures        = figures;
    this._launchError    = null;
    this._launchSuccess  = false;
    this._launchResult   = null;
    this._launching      = false;
    this._selectedNodeId = null;
    this._saveAsMode     = false;
    this._saveAsName     = '';
    this._userPresets    = loadUserPresets(repo);

    this._el.classList.add('is-open');

    void this._loadPhases();

    // Fetch built-in preset summaries from the API.
    this._presetsLoading = true;
    this._syncDOM();
    this._renderPresets();

    void fetch('/api/org-presets')
      .then(async r => {
        if (r.ok) this._builtInPresets = await r.json() as ApiPresetSummary[];
      })
      .catch(() => { /* non-critical — grid will be empty */ })
      .finally(() => {
        this._presetsLoading = false;
        this._syncDOM();
        this._renderPresets();
      });

    // If there is a saved in-progress session for this initiative, jump
    // straight to the canvas.  Otherwise show the preset picker first.
    const saved = loadFromStorage(repo, label);
    if (saved) {
      this._root        = saved;
      this._presetsOpen = false;
      this._openCanvas();
    } else {
      this._root           = null;
      this._activePresetId = null;
      this._presetsOpen    = true;
      this._syncDOM();
      this._renderPresets();
    }
  }

  close(): void {
    this._el.classList.remove('is-open');
  }

  // ── Preset management ──────────────────────────────────────────────────────

  loadPreset(id: string): void {
    if (id.startsWith('builtin-')) {
      void (async () => {
        const resp = await fetch(`/api/org-presets/${id}`);
        if (!resp.ok) return;
        const detail        = await resp.json() as ApiPresetDetail;
        this._root          = buildTree(detail.template);
        this._activePresetId = id;
        this._presetsOpen   = false;
        this._selectedNodeId = null;
        this._openCanvas();
      })();
      return;
    }
    // User-saved preset — already in memory.
    const user = this._userPresets.find(p => p.id === id);
    if (!user) return;
    this._root           = restoreNode(JSON.parse(JSON.stringify(user.tree)) as Partial<OrgNode>);
    this._activePresetId = id;
    this._presetsOpen    = false;
    this._selectedNodeId = null;
    this._saveToStorage();
    this._openCanvas();
  }

  loadBlank(): void {
    this._root           = makeNode('', '');
    this._activePresetId = null;
    this._presetsOpen    = false;
    this._selectedNodeId = null;
    clearStorage(this._repo, this._initiative);
    this._openCanvas();
  }

  saveCurrentAsPreset(): void {
    if (!this._saveAsName.trim() || !this._root) return;
    const preset: OrgPreset = {
      id:        `user_${Date.now()}`,
      name:      this._saveAsName.trim(),
      tree:      this._root,
      updatedAt: new Date().toISOString(),
    };
    this._userPresets    = [...this._userPresets, preset];
    saveUserPresets(this._repo, this._userPresets);
    this._activePresetId = preset.id;
    this._saveAsMode     = false;
    this._saveAsName     = '';
    this._syncDOM();
  }

  updateActivePreset(): void {
    if (!this._activePresetId || !this._root || this._activePresetIsBuiltIn()) return;
    const idx = this._userPresets.findIndex(p => p.id === this._activePresetId);
    if (idx === -1) return;
    const updated: OrgPreset = {
      ...this._userPresets[idx],
      tree:      this._root,
      updatedAt: new Date().toISOString(),
    };
    const next = [...this._userPresets];
    next[idx]         = updated;
    this._userPresets = next;
    saveUserPresets(this._repo, this._userPresets);
  }

  deletePreset(id: string, e: MouseEvent): void {
    e.stopPropagation();
    this._userPresets = this._userPresets.filter(p => p.id !== id);
    saveUserPresets(this._repo, this._userPresets);
    if (this._activePresetId === id) this._activePresetId = null;
    this._renderPresets();
    this._syncDOM();
  }

  private _openCanvas(): void {
    this._syncDOM();
    this._renderPresets();
    queueMicrotask(() => {
      this._container = this._el.querySelector<HTMLElement>('#od-canvas');
      requestAnimationFrame(() => {
        this._render();
        if (this._root && !this._root.role) {
          this._openEditor(this._root.id, null, 'coordinator');
        }
      });
    });
  }

  // ── Phase loading ──────────────────────────────────────────────────────────

  private async _loadPhases(): Promise<void> {
    try {
      const url = `/api/dispatch/context?label=${encodeURIComponent(this._initiative)}&repo=${encodeURIComponent(this._repo)}`;
      const res = await fetch(url);
      if (res.ok) {
        const data   = await res.json() as ContextResponse;
        this._phases = data.phases ?? [];
      }
    } catch {
      // Non-critical — scope picker just won't show phases.
    }
  }

  // ── Tree mutations ─────────────────────────────────────────────────────────

  private addChild(parentId: string): void {
    if (!this._root) return;
    const parent = findNode(this._root, parentId);
    if (!parent) return;
    const child = makeNode('', '');
    parent.children.push(child);
    this._render();
    this._saveToStorage();
    const types = availableChildTypes(parent.role);
    const defaultType: 'coordinator' | 'worker' =
      isCoordinator(parent.role) && types.includes('coordinator') ? 'coordinator' : 'worker';
    this._openEditor(child.id, parent.role, defaultType);
  }

  private removeNodeById(id: string): void {
    if (!this._root || id === this._root.id) return;
    pruneNode(this._root, id);
    if (this._selectedNodeId === id) this._selectedNodeId = null;
    this._render();
    this._saveToStorage();
    this._syncDOM();
  }

  private selectNodeById(id: string): void {
    if (!this._root) return;
    const node = findNode(this._root, id);
    if (!node) return;
    const parentRole = this._findParentRole(id);
    const type: 'coordinator' | 'worker' = node.role
      ? (isCoordinator(node.role) ? 'coordinator' : 'worker')
      : 'coordinator';
    this._openEditor(id, parentRole, type);
  }

  private _findParentRole(childId: string): string | null {
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
  }

  private _openEditor(id: string, parentRole: string | null, type: 'coordinator' | 'worker'): void {
    if (!this._root) return;
    const node = findNode(this._root, id);
    if (!node) return;
    this._selectedNodeId = id;
    this._editParentRole = parentRole;
    this._editType       = type;
    this._editRole       = node.role;
    this._editFigure     = node.figure;
    this._editScope      = node.scope;
    this._editScopeLabel = node.scopeLabel;
    this._syncDOM();
    this._renderRoleOptions();
    this._renderFigureOptions();
  }

  private _onTypeChange(): void {
    this._editRole   = '';
    this._editFigure = '';
    this._syncDOM();
    this._renderRoleOptions();
    this._renderFigureOptions();
  }

  private _onRoleChange(): void {
    if (!this._editFigure) return;
    const compatible = this._roleFigureMap[this._editRole];
    if (compatible && compatible.length > 0 && !compatible.includes(this._editFigure)) {
      this._editFigure = '';
    }
  }

  applyEdit(): void {
    if (!this._root) return;
    const node = findNode(this._root, this._selectedNodeId ?? '');
    if (!node) return;
    node.role       = this._editRole;
    node.figure     = this._editFigure;
    node.scope      = this._editScope;
    node.scopeLabel = this._editScope === 'phase' ? this._editScopeLabel : '';
    this._selectedNodeId = null;
    this._render();
    this._saveToStorage();
    this._syncDOM();
  }

  cancelEdit(): void {
    if (this._selectedNodeId && this._root) {
      const node = findNode(this._root, this._selectedNodeId);
      if (node && !node.role) {
        if (node.id === this._root.id) {
          // Blank root cancelled — go back to preset picker.
          this._presetsOpen    = true;
          this._selectedNodeId = null;
          this._syncDOM();
          this._renderPresets();
          return;
        }
        // Blank child cancelled — remove it.
        pruneNode(this._root, this._selectedNodeId);
        this._render();
        this._saveToStorage();
      }
    }
    this._selectedNodeId = null;
    this._syncDOM();
  }

  clearDesign(): void {
    clearStorage(this._repo, this._initiative);
    this._root           = null;
    this._selectedNodeId = null;
    this._activePresetId = null;
    this._launchSuccess  = false;
    this._launchResult   = null;
    this._launchError    = null;
    this._saveAsMode     = false;
    this._saveAsName     = '';
    this._presetsOpen    = true;
    this._syncDOM();
    this._renderPresets();
  }

  // ── localStorage ──────────────────────────────────────────────────────────

  private _saveToStorage(): void {
    if (this._root) saveToStorage(this._repo, this._initiative, this._root);
  }

  // ── D3 render ─────────────────────────────────────────────────────────────

  private _render(): void {
    if (!this._container || !this._root) return;
    renderD3(
      this._root,
      this._container,
      this._figures,
      this._selectedNodeId,
      id => { this.addChild(id); },
      id => { this.removeNodeById(id); },
      id => { this.selectNodeById(id); },
    );
    this._syncDOM();
  }

  // ── Launch ─────────────────────────────────────────────────────────────────

  async launch(): Promise<void> {
    if (!this._launchReady() || !this._root) return;
    this._launching   = true;
    this._launchError = null;
    this._syncDOM();

    const payload = {
      label:                   this._initiative,
      scope:                   this._root.scope,
      scope_label:             this._root.scope === 'phase' ? this._root.scopeLabel : undefined,
      repo:                    this._repo,
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
        this._launchError = (data as DispatchError).detail ?? `Error ${res.status}`;
      } else {
        const dispatched     = data as DispatchResponse;
        this._launchResult   = dispatched;
        this._launchSuccess  = true;
        this._root.launched  = true;
        this._root.runId     = dispatched.run_id;
        this._render();
      }
    } catch (err) {
      this._launchError = `Network error: ${err instanceof Error ? err.message : String(err)}`;
    } finally {
      this._launching = false;
      this._syncDOM();
    }
  }
}

// ── Entry point ───────────────────────────────────────────────────────────────

export function initOrgDesigner(): void {
  const el = document.getElementById('od-overlay');
  if (el instanceof HTMLElement) new OrgDesigner(el);
}
