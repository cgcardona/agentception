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
 * @param {string} role - Role slug (empty string = not yet configured).
 * @param {string} figure - Cognitive arch figure slug ('' for role default).
 * @returns {{id: string, role: string, figure: string, children: Array}}
 */
function makeNode(role = '', figure = '') {
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

const NODE_W      = 240;  // node card width in px
const NODE_H      = 108;  // node card height in px
const NODE_GAP_X  = 48;   // horizontal gap between sibling cards
const NODE_GAP_Y  = 72;   // vertical gap between parent and child row

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
    .classed('od-node--selected',    d => d.data.id === selectedId)
    .classed('od-node--coordinator', d => !!d.data.role && isCoordinator(d.data.role))
    .classed('od-node--worker',      d => !!d.data.role && !isCoordinator(d.data.role))
    .classed('od-node--pending',     d => !d.data.role)
    .html(d => {
      const configured = !!d.data.role;
      const tier    = configured ? (isCoordinator(d.data.role) ? 'coordinator' : 'worker') : 'pending';
      const roleLbl = configured ? roleLabel(d.data.role) : '— define role —';
      const figName = configured
        ? (d.data.figure ? (figMap.get(d.data.figure) ?? d.data.figure) : 'role default')
        : '';
      const removeBtn = d.depth > 0
        ? `<button class="od-node__btn od-node__btn--remove" data-id="${d.data.id}" title="Remove node">×</button>`
        : '';
      return `
        <div class="od-node__tier od-node__tier--${tier}">${tier}</div>
        <div class="od-node__role ${configured ? '' : 'od-node__role--empty'}">${roleLbl}</div>
        ${configured ? `<div class="od-node__figure">${figName}</div>` : ''}
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
    // ── Overlay state ─────────────────────────────────────────────────────────

    open: false,
    initiative: '',
    repo: '',
    figures: /** @type {Array<{id: string, name: string}>} */ ([]),

    // ── Node editor state ────────────────────────────────────────────────────
    // editType is the FIRST decision: coordinator or worker.
    // editRole is filtered by editType so the dropdown only shows relevant roles.

    selectedNodeId: /** @type {string|null} */ (null),
    editType: 'coordinator',   // 'coordinator' | 'worker'
    editRole: '',
    editFigure: '',

    // ── Submission state ──────────────────────────────────────────────────────

    launching: false,
    launchError: /** @type {string|null} */ (null),
    launchSuccess: false,
    /** @type {{run_id: string, batch_id: string}|null} */
    launchResult: null,

    // ── Internal tree (NOT Alpine-reactive — D3 owns re-render) ──────────────

    _root: /** @type {object|null} */ (null),
    _container: /** @type {HTMLElement|null} */ (null),

    // ── Computed ─────────────────────────────────────────────────────────────

    get selectedNode() {
      if (!this.selectedNodeId || !this._root) return null;
      return findNode(this._root, this.selectedNodeId);
    },

    /** Role groups filtered to coordinator or worker based on editType. */
    get filteredRoleGroups() {
      return ROLE_GROUPS.filter(g => g.type === this.editType);
    },

    /** True when the root node has a role configured and we can launch. */
    get launchReady() {
      return !!(this._root && this._root.role && !this.launching && !this.launchSuccess);
    },

    get launchPreviewText() {
      if (!this._root || !this._root.role) return 'Configure root node first';
      const figMap    = new Map(this.figures.map(f => [f.id, f.name]));
      const figName   = this._root.figure
        ? (figMap.get(this._root.figure) ?? this._root.figure)
        : 'role default';
      const nodeCount = countNodes(this._root) - 1;
      const childNote = nodeCount > 0
        ? ` + ${nodeCount} child${nodeCount === 1 ? '' : 'ren'}`
        : '';
      return `Launch ${roleLabel(this._root.role)} (${figName})${childNote} →`;
    },

    // ── Lifecycle ─────────────────────────────────────────────────────────────

    /**
     * Open the designer for *label*.  Creates a blank root node and
     * immediately opens the editor so the first decision is coordinator vs worker.
     */
    openDesigner(label, repo, figures) {
      this.initiative    = label;
      this.repo          = repo;
      this.figures       = figures;
      this.launchError   = null;
      this.launchSuccess = false;
      this.launchResult  = null;
      this.launching     = false;
      this.open          = true;

      // Blank root — no role pre-seeded.  Editor opens immediately.
      this._root = makeNode('', '');

      this.$nextTick(() => {
        this._container = document.getElementById('od-canvas');
        // Defer render by one frame so the overlay has its final dimensions
        // before D3 reads clientWidth for centering.
        requestAnimationFrame(() => {
          this._render();
          // Open editor for the blank root so user makes the first choice now.
          this._openEditor(this._root.id, 'coordinator');
        });
      });
    },

    close() {
      this.open = false;
    },

    // ── Tree mutations ────────────────────────────────────────────────────────

    addChild(parentId) {
      const parent = findNode(this._root, parentId);
      if (!parent) return;
      const child = makeNode('', '');
      parent.children.push(child);
      this._render();
      // Open editor for the new child immediately — default to worker.
      this._openEditor(child.id, 'worker');
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
      // Derive coordinator/worker from the existing role (or default coordinator).
      const type = node.role ? (isCoordinator(node.role) ? 'coordinator' : 'worker') : 'coordinator';
      this._openEditor(id, type);
    },

    /** Internal: open the editor panel for a node, setting the type radio. */
    _openEditor(id, type) {
      const node = findNode(this._root, id);
      if (!node) return;
      this.selectedNodeId = id;
      this.editType       = type;
      this.editRole       = node.role;
      this.editFigure     = node.figure;
    },

    /** Called when the Coordinator/Worker radio changes — clear the role. */
    onTypeChange() {
      this.editRole = '';
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
      // If the node being edited has no role yet (new blank node), remove it.
      if (this.selectedNodeId && this._root) {
        const node = findNode(this._root, this.selectedNodeId);
        if (node && !node.role && node.id !== this._root.id) {
          pruneNode(this._root, this.selectedNodeId);
          this._render();
        }
        // If it's the blank root, just close the overlay entirely.
        if (node && !node.role && node.id === this._root.id) {
          this.close();
          return;
        }
      }
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
      if (!this.launchReady) return;
      this.launching   = true;
      this.launchError = null;

      const payload = {
        label:                   this.initiative,
        scope:                   'full_initiative',
        repo:                    this.repo,
        role:                    this._root.role,
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
