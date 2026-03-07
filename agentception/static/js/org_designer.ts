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

  return `
    <div class="od-node__tier od-node__tier--${tier}">${tier}</div>
    <div class="od-node__role${configured ? '' : ' od-node__role--empty'}">${roleLbl}</div>
    ${configured ? `<div class="od-node__figure">${figName}</div>` : ''}
    ${scopeNote}
    <div class="od-node__actions">
      <button class="od-node__btn od-node__btn--add"  data-id="${node.id}" title="Add child">+ child</button>
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

  // ── Node editor
  selectedNodeId: string | null;
  editType: 'coordinator' | 'worker';
  editParentRole: string | null;
  editRole: string;
  editFigure: string;
  editScope: 'full_initiative' | 'phase';
  editScopeLabel: string;
  phases: PhaseItem[];

  // ── Submission
  launching: boolean;
  launchError: string | null;
  launchSuccess: boolean;
  launchResult: DispatchResponse | null;

  // ── Internal (D3 / mutable tree — not Alpine-reactive)
  _root: OrgNode | null;
  _container: HTMLElement | null;

  // ── Static data exposed to template
  roleGroups: RoleGroup[];

  // ── Computed
  readonly selectedNode: OrgNode | null;
  readonly filteredRoleGroups: RoleGroup[];
  readonly availableEditTypes: Array<'coordinator' | 'worker'>;
  readonly launchReady: boolean;
  readonly launchPreviewText: string;

  // ── Methods
  openDesigner(label: string, repo: string, figures: FigureItem[]): void;
  close(): void;
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
}

// ── Alpine component factory ──────────────────────────────────────────────────

export function orgDesigner(): OrgDesignerComponent {
  return {
    // ── Overlay state ─────────────────────────────────────────────────────────
    open:       false,
    initiative: '',
    repo:       '',
    figures:    [],

    // ── Node editor state ─────────────────────────────────────────────────────
    selectedNodeId: null,
    editType:       'coordinator',
    editParentRole: null,
    editRole:       '',
    editFigure:     '',
    editScope:      'full_initiative',
    editScopeLabel: '',
    phases:         [],

    // ── Submission state ──────────────────────────────────────────────────────
    launching:    false,
    launchError:  null,
    launchSuccess:false,
    launchResult: null,

    // ── Internal ──────────────────────────────────────────────────────────────
    _root:      null,
    _container: null,
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

    /** Which type tabs are valid given the parent — hides irrelevant radio options. */
    get availableEditTypes(): Array<'coordinator' | 'worker'> {
      if (!this.editParentRole) return ['coordinator', 'worker'];
      return availableChildTypes(this.editParentRole);
    },

    get launchReady(): boolean {
      return !!(this._root && this._root.role && !this.launching && !this.launchSuccess);
    },

    get launchPreviewText(): string {
      if (!this._root || !this._root.role) return 'Configure root node first';
      const figMap  = new Map(this.figures.map(f => [f.id, f.name]));
      const figName = this._root.figure ? (figMap.get(this._root.figure) ?? this._root.figure) : 'role default';
      const extra   = countNodes(this._root) - 1;
      const note    = extra > 0 ? ` + ${extra} child${extra === 1 ? '' : 'ren'}` : '';
      return `Launch ${roleLabel(this._root.role)} (${figName})${note} →`;
    },

    // ── Lifecycle ──────────────────────────────────────────────────────────────

    openDesigner(label: string, repo: string, figures: FigureItem[]): void {
      this.initiative   = label;
      this.repo         = repo;
      this.figures      = figures;
      this.launchError  = null;
      this.launchSuccess= false;
      this.launchResult = null;
      this.launching    = false;
      this.selectedNodeId = null;

      // Restore from localStorage or start fresh.
      const saved = loadFromStorage(repo, label);
      this._root = saved ?? makeNode('', '');
      this.open  = true;

      void this._loadPhases();

      void (this as unknown as AlpineMagics).$nextTick(() => {
        this._container = document.getElementById('od-canvas');
        requestAnimationFrame(() => {
          this._render();
          // Open editor for root immediately when starting fresh.
          if (!saved && this._root) {
            this._openEditor(this._root.id, null, 'coordinator');
          }
        });
      });
    },

    close(): void {
      this.open = false;
    },

    // ── Phase loading ─────────────────────────────────────────────────────────

    async _loadPhases(): Promise<void> {
      try {
        const url = `/api/dispatch/context?label=${encodeURIComponent(this.initiative)}&repo=${encodeURIComponent(this.repo)}`;
        const res = await fetch(url);
        if (res.ok) {
          const data = await res.json() as ContextResponse;
          this.phases = data.phases ?? [];
        }
      } catch {
        // Non-critical — scope picker just won't show phases.
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
      // Default child to first available type for this parent.
      const types = availableChildTypes(parent.role);
      const defaultType: 'coordinator' | 'worker' = types.includes('worker') ? 'worker' : (types[0] ?? 'worker');
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
      this.selectedNodeId = id;
      this.editParentRole = parentRole;
      this.editType       = type;
      this.editRole       = node.role;
      this.editFigure     = node.figure;
      this.editScope      = node.scope;
      this.editScopeLabel = node.scopeLabel;
    },

    onTypeChange(): void {
      this.editRole = '';
    },

    applyEdit(): void {
      if (!this._root) return;
      const node = findNode(this._root, this.selectedNodeId ?? '');
      if (!node) return;
      node.role       = this.editRole;
      node.figure     = this.editFigure;
      node.scope      = this.editScope;
      node.scopeLabel = this.editScope === 'phase' ? this.editScopeLabel : '';
      this.selectedNodeId = null;
      this._render();
      this._saveToStorage();
    },

    cancelEdit(): void {
      if (this.selectedNodeId && this._root) {
        const node = findNode(this._root, this.selectedNodeId);
        if (node && !node.role) {
          if (node.id === this._root.id) {
            // Blank root cancelled — close the whole designer.
            this.close();
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

    /** Reset the designer to a blank slate and clear localStorage. */
    clearDesign(): void {
      clearStorage(this.repo, this.initiative);
      this._root = makeNode('', '');
      this.selectedNodeId = null;
      this.launchSuccess  = false;
      this.launchResult   = null;
      this.launchError    = null;
      this._render();
      if (this._root) this._openEditor(this._root.id, null, 'coordinator');
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
          this.launchError = (data as DispatchError).detail ?? `Error ${res.status}`;
        } else {
          const dispatched    = data as DispatchResponse;
          this.launchResult   = dispatched;
          this.launchSuccess  = true;
          // Mark root as launched — D3 re-renders it green with run_id.
          this._root.launched = true;
          this._root.runId    = dispatched.run_id;
          this._render();
        }
      } catch (err) {
        this.launchError = `Network error: ${err instanceof Error ? err.message : String(err)}`;
      } finally {
        this.launching = false;
      }
    },
  } as OrgDesignerComponent;
}
