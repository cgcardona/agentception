'use strict';

/**
 * Org Designer — visual tree builder for agent hierarchies.
 *
 * Architecture:
 *   - D3 v7 owns the canvas: tree layout math, SVG bezier edges, and
 *     absolutely-positioned HTML node cards.
 *   - Alpine.js manages only the overlay open/close and the side-panel node
 *     editor for the selected node.
 *   - Tree data is a plain mutable JS object tree (NOT Alpine-reactive) to
 *     avoid recursion limits.  D3 re-renders the entire tree on every mutation.
 *
 * Entry points (called from build.html):
 *   Alpine.store('orgDesigner')  — not a store; registered as a component.
 *   orgDesigner()                — returns the Alpine data object.
 */

// ── Role catalog ──────────────────────────────────────────────────────────────

/** @type {Array<{label: string, type: 'coordinator'|'worker', roles: Array<{slug: string, label: string}>}>} */
const ROLE_GROUPS = [
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
      { slug: 'engineering-coordinator',    label: 'Engineering Manager' },
      { slug: 'qa-coordinator',             label: 'QA Lead' },
      { slug: 'ml-coordinator',             label: 'ML Coordinator' },
      { slug: 'design-coordinator',         label: 'Design Lead' },
      { slug: 'security-coordinator',       label: 'Security Lead' },
      { slug: 'platform-coordinator',       label: 'Platform Coordinator' },
      { slug: 'infrastructure-coordinator', label: 'Infrastructure Lead' },
      { slug: 'data-coordinator',           label: 'Data Coordinator' },
      { slug: 'mobile-coordinator',         label: 'Mobile Lead' },
      { slug: 'product-coordinator',        label: 'Product Coordinator' },
    ],
  },
  {
    label: 'Engineering',
    type: 'worker',
    roles: [
      { slug: 'python-developer',     label: 'Python Developer' },
      { slug: 'typescript-developer', label: 'TypeScript Developer' },
      { slug: 'go-developer',         label: 'Go Developer' },
      { slug: 'rust-developer',       label: 'Rust Developer' },
      { slug: 'rails-developer',      label: 'Rails Developer' },
      { slug: 'systems-programmer',   label: 'Systems Programmer' },
      { slug: 'api-developer',        label: 'API Developer' },
      { slug: 'full-stack-developer', label: 'Full-Stack Developer' },
      { slug: 'data-engineer',        label: 'Data Engineer' },
      { slug: 'database-architect',   label: 'Database Architect' },
      { slug: 'architect',            label: 'Architect' },
    ],
  },
  {
    label: 'Frontend / Mobile',
    type: 'worker',
    roles: [
      { slug: 'frontend-developer', label: 'Frontend Developer' },
      { slug: 'react-developer',    label: 'React Developer' },
      { slug: 'ios-developer',      label: 'iOS Developer' },
      { slug: 'android-developer',  label: 'Android Developer' },
      { slug: 'mobile-developer',   label: 'Mobile Developer' },
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
      { slug: 'pr-reviewer',              label: 'PR Reviewer' },
      { slug: 'test-engineer',            label: 'Test Engineer' },
      { slug: 'devops-engineer',          label: 'DevOps Engineer' },
      { slug: 'site-reliability-engineer', label: 'SRE' },
    ],
  },
  {
    label: 'Other',
    type: 'worker',
    roles: [
      { slug: 'security-engineer',  label: 'Security Engineer' },
      { slug: 'technical-writer',   label: 'Technical Writer' },
      { slug: 'content-writer',     label: 'Content Writer' },
    ],
  },
];

/** Set of coordinator role slugs — used for tier badge and child defaults. */
const COORDINATOR_SLUGS = new Set([
  'ceo', 'cto', 'cpo', 'coo', 'cfo', 'ciso', 'cmo', 'csto', 'cdo',
  'engineering-coordinator', 'qa-coordinator', 'ml-coordinator',
  'design-coordinator', 'security-coordinator', 'platform-coordinator',
  'infrastructure-coordinator', 'data-coordinator', 'mobile-coordinator',
  'product-coordinator',
]);

/** True when *slug* belongs to the coordinator tier. */
function isCoordinator(slug) {
  return COORDINATOR_SLUGS.has(slug);
}

/** Human-readable label for *slug*, falling back to the slug itself. */
function roleLabel(slug) {
  for (const group of ROLE_GROUPS) {
    const match = group.roles.find(r => r.slug === slug);
    if (match) return match.label;
  }
  return slug;
}

// ── Tree node helpers ─────────────────────────────────────────────────────────

let _nodeCounter = 0;

/**
 * Create a new tree node.
 * @param {string} role - Role slug.
 * @param {string} figure - Cognitive arch figure slug ('' for role default).
 * @returns {{id: string, role: string, figure: string, children: Array}}
 */
function makeNode(role = 'cto', figure = '') {
  return { id: `n${++_nodeCounter}`, role, figure, children: [] };
}

/**
 * Find the node with *id* in the subtree rooted at *node*.
 * Returns null when not found.
 */
function findNode(node, id) {
  if (node.id === id) return node;
  for (const child of node.children) {
    const found = findNode(child, id);
    if (found) return found;
  }
  return null;
}

/**
 * Remove the node with *id* from *root*'s descendant list.
 * No-ops if *id* is the root itself.
 */
function pruneNode(root, id) {
  root.children = root.children.filter(c => c.id !== id);
  root.children.forEach(c => pruneNode(c, id));
}

/** Count all nodes in the subtree rooted at *node* (including *node*). */
function countNodes(node) {
  return 1 + node.children.reduce((s, c) => s + countNodes(c), 0);
}

// ── D3 canvas constants ───────────────────────────────────────────────────────

const NODE_W      = 210;  // node card width in px
const NODE_H      = 90;   // node card height in px
const NODE_GAP_X  = 32;   // horizontal gap between sibling cards
const NODE_GAP_Y  = 64;   // vertical gap between parent and child row

// ── D3 rendering ─────────────────────────────────────────────────────────────

/**
 * (Re)render the org tree inside *container* using D3 v7.
 *
 * Layout: d3.tree() with nodeSize computes (x, y) for every node.
 * Edges: SVG <path> elements drawn with d3.linkVertical().
 * Cards: absolutely-positioned <div> elements positioned by D3's coordinates.
 *
 * The function is idempotent — safe to call repeatedly on mutations.
 *
 * @param {object} rootData - Plain JS tree (see makeNode()).
 * @param {HTMLElement} container - The scrollable canvas div.
 * @param {Function} onAdd - (nodeId: string) => void
 * @param {Function} onRemove - (nodeId: string) => void
 * @param {Function} onSelect - (nodeId: string) => void
 * @param {string|null} selectedId - Currently selected node id (for highlight).
 * @param {Array<{id: string, name: string}>} figures - Figure catalog.
 */
function renderD3(rootData, container, onAdd, onRemove, onSelect, selectedId, figures) {
  // ── First-call bootstrap ──────────────────────────────────────────────────
  if (!container._d3svg) {
    container.style.position = 'relative';
    container.style.overflow = 'auto';

    const svgEl = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svgEl.style.cssText = 'position:absolute;top:0;left:0;pointer-events:none;overflow:visible;';
    container.appendChild(svgEl);
    container._d3svg = window.d3.select(svgEl);

    const cardLayerEl = document.createElement('div');
    cardLayerEl.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;';
    container.appendChild(cardLayerEl);
    container._d3cards = window.d3.select(cardLayerEl);
  }

  const svg       = container._d3svg;
  const cardLayer = container._d3cards;

  // ── Layout ────────────────────────────────────────────────────────────────
  const hierarchy  = window.d3.hierarchy(rootData, d => d.children.length ? d.children : null);
  const treeLayout = window.d3.tree().nodeSize([NODE_W + NODE_GAP_X, NODE_H + NODE_GAP_Y]);
  treeLayout(hierarchy);

  // Bounding box
  let minX = Infinity, maxX = -Infinity, maxY = -Infinity;
  hierarchy.each(d => {
    if (d.x < minX) minX = d.x;
    if (d.x > maxX) maxX = d.x;
    if (d.y > maxY) maxY = d.y;
  });

  const treeW    = maxX - minX + NODE_W + 80;
  const treeH    = maxY + NODE_H + 80;
  const canvasW  = Math.max(container.clientWidth || 900, treeW);
  const canvasH  = Math.max(400, treeH);
  const offsetX  = canvasW / 2 - (minX + maxX) / 2;
  const offsetY  = 48;

  container.style.minHeight = canvasH + 'px';
  svg.attr('width', canvasW).attr('height', canvasH);

  // ── Edges ─────────────────────────────────────────────────────────────────
  svg.selectAll('.od-link')
    .data(hierarchy.links())
    .join('path')
    .attr('class', 'od-link')
    .attr('d', window.d3.linkVertical()
      .x(d => d.x + offsetX)
      .y(d => d.y + offsetY + NODE_H)
    );

  // ── Node cards ────────────────────────────────────────────────────────────
  const figMap = new Map(figures.map(f => [f.id, f.name]));

  const cards = cardLayer.selectAll('.od-node')
    .data(hierarchy.descendants(), d => d.data.id);

  // Enter + update merged
  const all = cards.enter()
    .append('div')
    .attr('class', 'od-node')
    .merge(cards);

  all
    .style('left', d => (d.x + offsetX - NODE_W / 2) + 'px')
    .style('top',  d => (d.y + offsetY) + 'px')
    .classed('od-node--selected', d => d.data.id === selectedId)
    .classed('od-node--coordinator', d => isCoordinator(d.data.role))
    .classed('od-node--worker', d => !isCoordinator(d.data.role))
    .html(d => {
      const tier    = isCoordinator(d.data.role) ? 'coordinator' : 'worker';
      const figName = d.data.figure ? (figMap.get(d.data.figure) ?? d.data.figure) : 'role default';
      const removeBtn = d.depth > 0
        ? `<button class="od-node__btn od-node__btn--remove" data-id="${d.data.id}" title="Remove node">×</button>`
        : '';
      return `
        <div class="od-node__tier od-node__tier--${tier}">${tier}</div>
        <div class="od-node__role">${roleLabel(d.data.role)}</div>
        <div class="od-node__figure">${figName}</div>
        <div class="od-node__actions">
          <button class="od-node__btn od-node__btn--add"    data-id="${d.data.id}" title="Add child">+ child</button>
          <button class="od-node__btn od-node__btn--edit"   data-id="${d.data.id}" title="Edit node">edit</button>
          ${removeBtn}
        </div>`;
    });

  cards.exit().remove();

  // ── Event delegation ──────────────────────────────────────────────────────
  // Rebind on every render — .html() tears down old listeners automatically.
  cardLayer.node().querySelectorAll('.od-node__btn--add').forEach(btn => {
    btn.addEventListener('click', e => { e.stopPropagation(); onAdd(btn.dataset.id); });
  });
  cardLayer.node().querySelectorAll('.od-node__btn--remove').forEach(btn => {
    btn.addEventListener('click', e => { e.stopPropagation(); onRemove(btn.dataset.id); });
  });
  cardLayer.node().querySelectorAll('.od-node__btn--edit').forEach(btn => {
    btn.addEventListener('click', e => { e.stopPropagation(); onSelect(btn.dataset.id); });
  });
}

// ── Alpine component ──────────────────────────────────────────────────────────

export function orgDesigner() {
  return {
    // ── State ────────────────────────────────────────────────────────────────

    /** Overlay visibility. */
    open: false,
    /** Initiative label being designed for. */
    initiative: '',
    /** GitHub repo (owner/repo). */
    repo: '',
    /** Figure catalog loaded from the backend. */
    figures: /** @type {Array<{id: string, name: string}>} */ ([]),

    // Selected-node editor state
    selectedNodeId: /** @type {string|null} */ (null),
    editRole: '',
    editFigure: '',

    // Submission state
    launching: false,
    launchError: /** @type {string|null} */ (null),
    launchSuccess: false,
    /** @type {{run_id: string, batch_id: string}|null} */
    launchResult: null,

    // Internal D3 tree root (plain mutable JS object, NOT Alpine-reactive).
    _root: /** @type {object|null} */ (null),
    _container: /** @type {HTMLElement|null} */ (null),

    // Role groups exposed to the editor template.
    roleGroups: ROLE_GROUPS,

    // ── Computed ─────────────────────────────────────────────────────────────

    get selectedNode() {
      if (!this.selectedNodeId || !this._root) return null;
      return findNode(this._root, this.selectedNodeId);
    },

    get launchPreviewText() {
      if (!this._root) return 'Launch';
      const root      = this._root;
      const figMap    = new Map(this.figures.map(f => [f.id, f.name]));
      const figName   = root.figure ? (figMap.get(root.figure) ?? root.figure) : 'role default';
      const nodeCount = countNodes(root) - 1;
      const childNote = nodeCount > 0
        ? ` with ${nodeCount} child${nodeCount === 1 ? '' : 'ren'}`
        : '';
      return `Launch ${roleLabel(root.role)} (${figName})${childNote} →`;
    },

    // ── Lifecycle ────────────────────────────────────────────────────────────

    /**
     * Open the designer pre-seeded for *label*.
     * @param {string} label - Initiative label.
     * @param {string} repo  - ``owner/repo`` string.
     * @param {Array<{id: string, name: string}>} figures - Figure catalog.
     */
    openDesigner(label, repo, figures) {
      this.initiative    = label;
      this.repo          = repo;
      this.figures       = figures;
      this._root         = makeNode('cto', 'jeff_dean');
      this.selectedNodeId = null;
      this.launchError   = null;
      this.launchSuccess = false;
      this.launchResult  = null;
      this.launching     = false;
      this.open          = true;

      this.$nextTick(() => {
        this._container = document.getElementById('od-canvas');
        this._render();
      });
    },

    close() {
      this.open = false;
    },

    // ── Tree mutations ────────────────────────────────────────────────────────

    addChild(parentId) {
      const parent = findNode(this._root, parentId);
      if (!parent) return;
      // Default new child: coordinator gets a worker, worker gets another worker.
      const childRole = isCoordinator(parent.role) ? 'python-developer' : 'python-developer';
      parent.children.push(makeNode(childRole, ''));
      this._render();
    },

    removeNodeById(id) {
      if (!this._root || id === this._root.id) return;
      pruneNode(this._root, id);
      if (this.selectedNodeId === id) this.selectedNodeId = null;
      this._render();
    },

    selectNodeById(id) {
      const node = findNode(this._root, id);
      if (!node) return;
      this.selectedNodeId = id;
      this.editRole       = node.role;
      this.editFigure     = node.figure;
    },

    applyEdit() {
      const node = findNode(this._root, this.selectedNodeId);
      if (!node) return;
      node.role   = this.editRole;
      node.figure = this.editFigure;
      this.selectedNodeId = null;
      this._render();
    },

    cancelEdit() {
      this.selectedNodeId = null;
    },

    // ── Internal render ───────────────────────────────────────────────────────

    _render() {
      if (!this._container || !this._root) return;
      renderD3(
        this._root,
        this._container,
        id => this.addChild(id),
        id => this.removeNodeById(id),
        id => this.selectNodeById(id),
        this.selectedNodeId,
        this.figures,
      );
    },

    // ── Launch ────────────────────────────────────────────────────────────────

    async launch() {
      if (!this._root || this.launching) return;
      this.launching  = true;
      this.launchError = null;

      const payload = {
        label:                  this.initiative,
        scope:                  'full_initiative',
        repo:                   this.repo,
        role:                   this._root.role,
        cognitive_arch_override: this._root.figure || null,
      };

      try {
        const res  = await fetch('/api/dispatch/label', {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok) {
          this.launchError = data.detail ?? `Error ${res.status}`;
        } else {
          this.launchResult  = data;
          this.launchSuccess = true;
        }
      } catch (err) {
        this.launchError = `Network error: ${err instanceof Error ? err.message : String(err)}`;
      } finally {
        this.launching = false;
      }
    },
  };
}
