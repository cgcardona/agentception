/**
 * Org Chart D3 tree visualization.
 *
 * Fetches GET /api/org/tree and renders a vertical D3 tree inside
 * #org-tree-panel.  Matches the visual style of dag.js: same PALETTE,
 * same font stack, same link stroke, same zoom behaviour.
 *
 * Node cards show: role name, tier badge, assigned phase chips,
 * and the first two compatible-figure avatars from the taxonomy.
 *
 * Clicking a node navigates to /roles/<slug> (the Role Studio editor).
 *
 * D3 is loaded from CDN in the page's {% block head %} — this module
 * guards against d3 being absent so the page degrades gracefully.
 */

'use strict';

// ── D3 minimal type declarations (CDN global) ─────────────────────────────────

interface D3ZoomBehavior {
  scaleExtent(extent: [number, number]): this;
  on(event: string, listener: (ev: D3ZoomEvent) => void): this;
  transform(selection: D3Selection, transform: D3ZoomIdentity): void;
}

interface D3ZoomEvent {
  transform: D3ZoomIdentity;
}

interface D3ZoomIdentity {
  translate(x: number, y: number): this;
  scale(k: number): this;
}

interface D3HierarchyDescendant {
  data: TreeNodeData;
  x: number;
  y: number;
  depth: number;
  parent: D3HierarchyDescendant | null;
  descendants(): D3HierarchyDescendant[];
  links(): D3HierarchyLink[];
  each(fn: (d: D3HierarchyDescendant) => void): void;
}

interface D3HierarchyLink {
  source: D3HierarchyDescendant;
  target: D3HierarchyDescendant;
}

interface D3Selection {
  append(tag: string): this;
  attr(name: string, value: string | number | ((d: D3HierarchyDescendant) => string | number)): this;
  call(behavior: D3ZoomBehavior): this;
  call(fn: (selection: D3Selection, transform: D3ZoomIdentity) => void, arg: D3ZoomIdentity): this;
  data<T>(arr: T[]): this;
  filter(fn: (d: D3HierarchyDescendant) => boolean): this;
  join(tag: string): this;
  on(event: string, handler: (ev: MouseEvent, d: D3HierarchyDescendant) => void): this;
  on(event: string, handler: (ev: MouseEvent) => void): this;
  on(event: string, handler: () => void): this;
  select(selector: string): this;
  selectAll(selector: string): this;
  style(name: string, value: string | ((d: D3HierarchyDescendant) => string)): this;
  text(value: string | ((d: D3HierarchyDescendant) => string)): this;
  transition(): this;
  duration(ms: number): this;
  each(fn: (this: SVGElement, d: D3HierarchyDescendant) => void): this;
}

interface D3Lib {
  hierarchy(root: TreeNodeData): D3HierarchyDescendant;
  tree(): D3TreeLayout;
  zoom(): D3ZoomBehavior;
  select(el: Element | null): D3Selection;
  zoomIdentity: D3ZoomIdentity;
}

interface D3TreeLayout {
  nodeSize(size: [number, number]): this;
  separation(fn: (a: D3HierarchyDescendant, b: D3HierarchyDescendant) => number): this;
  (root: D3HierarchyDescendant): void;
}

declare const d3: D3Lib;

// ── Domain types ──────────────────────────────────────────────────────────────

/** The shape of a node in the hierarchy tree used by D3. */
interface TreeNodeData {
  name: string;
  id: string;
  tier: string;
  slug: string | null;
  figures: string[];
  assigned_phases: string[];
  children: TreeNodeData[];
}

/** Shape of the raw API response from GET /api/org/tree. */
interface OrgTreeApiNode {
  name: string;
  id: string;
  tier: string;
  children?: OrgTreeChildGroup[];
}

/** A tier-group child (e.g. "leadership", "workers") within the API response. */
interface OrgTreeChildGroup {
  name: string;
  id: string;
  tier: string;
  roles?: OrgTreeRole[];
}

/** A leaf role entry within a tier-group. */
interface OrgTreeRole {
  name: string;
  slug: string;
  tier: string;
  figures?: string[];
  assigned_phases?: string[];
}

// ── Visual constants (mirror dag.js) ─────────────────────────────────────────

const PALETTE: string[] = [
  '#3b82f6', '#6366f1', '#14b8a6', '#22c55e',
  '#f97316', '#ef4444', '#a855f7', '#06b6d4', '#eab308', '#ec4899',
];

const TIER_COLORS: Record<string, string> = {
  'C-Suite': '#6366f1',
  'VP':      '#14b8a6',
  'Worker':  '#3b82f6',
  'org':     '#a855f7',
};

const CARD_W   = 160;
const CARD_H   = 80;
const DX       = 200;
const DY       = 130;

// ── Module state ─────────────────────────────────────────────────────────────

let _svg:    D3Selection | null    = null;
let _g:      D3Selection | null    = null;
let _zoom:   D3ZoomBehavior | null = null;
let _width   = 900;
let _height  = 520;

// ── Entry point ───────────────────────────────────────────────────────────────

/**
 * Initialise the org chart tree.  Called once the DOM is ready.
 * Fetches /api/org/tree and renders the D3 tree; shows a placeholder
 * when no active preset is selected (HTTP 404 from the endpoint).
 */
export async function initOrgChartTree(): Promise<void> {
  if (typeof d3 === 'undefined') {
    _showMessage('D3 library not loaded — tree unavailable.');
    return;
  }

  const panel = document.getElementById('org-tree-panel');
  if (!panel) return;

  _showMessage('Loading org tree…');

  let data: OrgTreeApiNode;
  try {
    const resp = await fetch('/api/org/tree');
    if (resp.status === 404) {
      _showMessage('No active preset selected. Choose one on the left to see the org tree.');
      return;
    }
    if (!resp.ok) {
      _showMessage(`Failed to load org tree (HTTP ${resp.status}).`);
      return;
    }
    data = await resp.json() as OrgTreeApiNode;
  } catch {
    _showMessage('Network error loading org tree.');
    return;
  }

  _clearPanel();
  _render(panel, data);
}

// ── Render ────────────────────────────────────────────────────────────────────

function _render(panel: HTMLElement, rootData: OrgTreeApiNode): void {
  const rect   = panel.getBoundingClientRect();
  _width  = Math.max(rect.width  || 900, 400);
  _height = Math.max(rect.height || 520, 400);

  const hierarchy = _buildHierarchy(rootData);
  const root      = d3.hierarchy(hierarchy);

  const treeLayout = d3.tree()
    .nodeSize([DX, DY])
    .separation((a, b) => (a.parent === b.parent ? 1.2 : 1.8));

  treeLayout(root);

  let x0 = Infinity, x1 = -Infinity;
  root.each(d => {
    if (d.x < x0) x0 = d.x;
    if (d.x > x1) x1 = d.x;
  });

  _svg = d3.select(panel)
    .append('svg')
    .attr('class', 'org-tree-svg')
    .attr('width',  '100%')
    .attr('height', _height)
    .attr('aria-label', 'Org chart tree visualization');

  _zoom = d3.zoom()
    .scaleExtent([0.1, 4])
    .on('zoom', (ev: D3ZoomEvent) => {
      if (_g) _g.attr('transform', String(ev.transform));
    });

  _svg.call(_zoom);

  _g = _svg.append('g').attr('class', 'org-tree-container');

  // Arrow marker
  _svg.append('defs').append('marker')
    .attr('id', 'org-arrow')
    .attr('viewBox', '0 0 10 10')
    .attr('refX', 5)
    .attr('refY', 5)
    .attr('markerWidth', 5)
    .attr('markerHeight', 5)
    .attr('orient', 'auto-start-reverse')
    .append('path')
    .attr('d', 'M 0 0 L 10 5 L 0 10 z')
    .attr('fill', '#6b7280');

  // ── Links ─────────────────────────────────────────────────────────────────
  _g.append('g')
    .attr('class', 'org-tree-links')
    .selectAll('path')
    .data(root.links())
    .join('path')
    .attr('d', d => _elbow(d as unknown as D3HierarchyLink))
    .attr('fill', 'none')
    .attr('stroke', '#6b7280')
    .attr('stroke-width', 1.5)
    .attr('opacity', 0.6)
    .attr('marker-end', 'url(#org-arrow)');

  // ── Nodes ─────────────────────────────────────────────────────────────────
  const nodeGroup = _g.append('g').attr('class', 'org-tree-nodes');

  const node = nodeGroup
    .selectAll('g.org-node')
    .data(root.descendants())
    .join('g')
    .attr('class', 'org-node')
    .attr('transform', d => `translate(${d.x - CARD_W / 2},${d.y - CARD_H / 2})`)
    .style('cursor', d => d.data.slug ? 'pointer' : 'default')
    .on('click', (_ev: MouseEvent, d: D3HierarchyDescendant) => {
      if (d.data.slug) {
        window.location.href = `/roles/${d.data.slug}`;
      }
    });

  // Card background
  node.append('rect')
    .attr('width',  CARD_W)
    .attr('height', CARD_H)
    .attr('rx', 10)
    .attr('ry', 10)
    .attr('fill',   d => _cardFill(d.data))
    .attr('stroke', d => _cardStroke(d.data))
    .attr('stroke-width', 2)
    .attr('opacity', 0.92);

  // Role name or tier-group label
  node.append('text')
    .attr('x', CARD_W / 2)
    .attr('y', d => d.data.tier === 'org' ? CARD_H / 2 : 20)
    .attr('text-anchor', 'middle')
    .attr('dominant-baseline', d => d.data.tier === 'org' ? 'central' : 'auto')
    .attr('font-size', d => d.data.tier === 'org' ? '13px' : '11px')
    .attr('font-family', 'monospace')
    .attr('fill', '#fff')
    .attr('pointer-events', 'none')
    .text(d => d.data.name || '');

  // Tier badge — role leaf nodes only
  node.filter(d => !!d.data.slug)
    .append('rect')
    .attr('x', 8)
    .attr('y', 30)
    .attr('width', 60)
    .attr('height', 16)
    .attr('rx', 4)
    .attr('fill', d => TIER_COLORS[d.data.tier] ?? '#6b7280')
    .attr('opacity', 0.75);

  node.filter(d => !!d.data.slug)
    .append('text')
    .attr('x', 38)
    .attr('y', 41)
    .attr('text-anchor', 'middle')
    .attr('dominant-baseline', 'central')
    .attr('font-size', '8px')
    .attr('font-family', 'monospace')
    .attr('fill', '#fff')
    .attr('pointer-events', 'none')
    .text(d => d.data.tier || '');

  // Figure avatar chips (first 2)
  node.filter(d => !!d.data.slug && d.data.figures.length > 0)
    .each(function(this: SVGElement, d: D3HierarchyDescendant) {
      const g = d3.select(this);
      d.data.figures.slice(0, 2).forEach((fig: string, i: number) => {
        const cx = CARD_W - 24 - i * 22;
        const cy = 41;
        g.append('circle')
          .attr('cx', cx)
          .attr('cy', cy)
          .attr('r', 9)
          .attr('fill', PALETTE[(fig.length + i) % PALETTE.length])
          .attr('opacity', 0.85)
          .attr('stroke', '#1e293b')
          .attr('stroke-width', 1);

        g.append('text')
          .attr('x', cx)
          .attr('y', cy)
          .attr('text-anchor', 'middle')
          .attr('dominant-baseline', 'central')
          .attr('font-size', '6px')
          .attr('font-family', 'monospace')
          .attr('fill', '#fff')
          .attr('pointer-events', 'none')
          .text(fig.slice(0, 2).toUpperCase());
      });
    });

  // Phase chips — wire up assigned_phases data
  node.filter(d => !!d.data.slug && d.data.assigned_phases.length > 0)
    .each(function(this: SVGElement, d: D3HierarchyDescendant) {
      const g = d3.select(this);
      d.data.assigned_phases.slice(0, 2).forEach((phase: string, i: number) => {
        const color  = PALETTE[i % PALETTE.length];
        const chipX  = 8 + i * 52;
        const chipLabel = phase.split('/').pop() ?? phase;
        g.append('rect')
          .attr('x', chipX)
          .attr('y', 56)
          .attr('width', 48)
          .attr('height', 14)
          .attr('rx', 3)
          .attr('fill', color)
          .attr('opacity', 0.7);
        g.append('text')
          .attr('x', chipX + 24)
          .attr('y', 63)
          .attr('text-anchor', 'middle')
          .attr('dominant-baseline', 'central')
          .attr('font-size', '7px')
          .attr('font-family', 'monospace')
          .attr('fill', '#fff')
          .attr('pointer-events', 'none')
          .text(chipLabel);
      });
    });

  // Tooltip
  const tooltip = document.getElementById('org-tree-tooltip');
  node.filter(d => !!d.data.slug)
    .on('mouseover', (_ev: MouseEvent, d: D3HierarchyDescendant) => {
      if (!tooltip) return;
      tooltip.innerHTML =
        `<span class="org-tooltip-name">${d.data.name}</span>` +
        `<span class="org-tooltip-tier">${d.data.tier}</span>` +
        (d.data.figures.length
          ? `<span class="org-tooltip-figs">Figures: ${d.data.figures.join(', ')}</span>`
          : '');
      tooltip.style.display = 'block';
    })
    .on('mousemove', (ev: MouseEvent) => {
      if (!tooltip) return;
      const panelRect = panel.getBoundingClientRect();
      tooltip.style.left = `${ev.clientX - panelRect.left + 12}px`;
      tooltip.style.top  = `${ev.clientY - panelRect.top  + 12}px`;
    })
    .on('mouseout', () => {
      if (tooltip) tooltip.style.display = 'none';
    });

  _fitView(root);

  const ro = new ResizeObserver(() => {
    const r = panel.getBoundingClientRect();
    _width  = Math.max(r.width  || 900, 400);
    _height = Math.max(r.height || 520, 400);
    if (_svg) _svg.attr('height', _height);
    _fitView(root);
  });
  ro.observe(panel);
}

// ── Helpers ───────────────────────────────────────────────────────────────────

/**
 * Convert the OrgTreeNode JSON into a D3-compatible hierarchy object.
 * Tier-group children become intermediate nodes; roles become leaf nodes.
 */
function _buildHierarchy(node: OrgTreeApiNode): TreeNodeData {
  const result: TreeNodeData = {
    name:            node.name,
    id:              node.id,
    tier:            node.tier,
    slug:            null,
    figures:         [],
    assigned_phases: [],
    children:        [],
  };

  for (const child of node.children ?? []) {
    const tierNode: TreeNodeData = {
      name:            child.name,
      id:              child.id,
      tier:            child.tier,
      slug:            null,
      figures:         [],
      assigned_phases: [],
      children:        (child.roles ?? []).map(role => ({
        name:            role.name,
        id:              role.slug,
        slug:            role.slug,
        tier:            role.tier,
        figures:         role.figures         ?? [],
        assigned_phases: role.assigned_phases ?? [],
        children:        [],
      })),
    };
    result.children.push(tierNode);
  }

  return result;
}

/** Elbow curve connector — top-to-bottom orthogonal path. */
function _elbow(d: D3HierarchyLink): string {
  const srcX = d.source.x;
  const srcY = d.source.y + CARD_H / 2;
  const tgtX = d.target.x;
  const tgtY = d.target.y - CARD_H / 2;
  const midY = (srcY + tgtY) / 2;
  return `M${srcX},${srcY} C${srcX},${midY} ${tgtX},${midY} ${tgtX},${tgtY}`;
}

/** Card fill colour: org root purple, tier groups dark, roles by tier. */
function _cardFill(d: TreeNodeData): string {
  if (d.tier === 'org') return '#4c1d95';
  if (!d.slug) return '#1e293b';
  return TIER_COLORS[d.tier] ?? '#6b7280';
}

/** Card border — highlight role nodes with a subtle stroke. */
function _cardStroke(d: TreeNodeData): string {
  if (d.tier === 'org') return '#7c3aed';
  if (!d.slug) return '#334155';
  return '#64748b';
}

/** Zoom/pan the SVG so the entire tree fits within the visible panel. */
function _fitView(root: D3HierarchyDescendant): void {
  if (!_svg || !_g || !_zoom) return;

  const nodes = root.descendants();
  const pad   = 48;
  const minX  = Math.min(...nodes.map(d => d.x)) - CARD_W / 2;
  const maxX  = Math.max(...nodes.map(d => d.x)) + CARD_W / 2;
  const minY  = Math.min(...nodes.map(d => d.y)) - CARD_H / 2;
  const maxY  = Math.max(...nodes.map(d => d.y)) + CARD_H / 2;
  const bboxW = Math.max(maxX - minX, 1);
  const bboxH = Math.max(maxY - minY, 1);
  const scale = Math.min(
    (_width  - pad * 2) / bboxW,
    (_height - pad * 2) / bboxH,
    2,
  );
  const tx = (_width  / 2) - (minX + bboxW / 2) * scale;
  const ty = pad - minY * scale;

  _svg.transition().duration(400)
    .call(
      _zoom.transform as unknown as (selection: D3Selection, transform: D3ZoomIdentity) => void,
      d3.zoomIdentity.translate(tx, ty).scale(scale),
    );
}

/** Replace panel contents with a centred message string. */
function _showMessage(msg: string): void {
  const panel = document.getElementById('org-tree-panel');
  if (!panel) return;
  panel.innerHTML = `<p class="org-tree-placeholder">${msg}</p>`;
}

/** Remove all children from the panel before (re-)rendering. */
function _clearPanel(): void {
  const panel = document.getElementById('org-tree-panel');
  if (panel) panel.innerHTML = '';
}
