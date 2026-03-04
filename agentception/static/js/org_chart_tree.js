'use strict';

/**
 * Org Chart D3 tree visualization.
 *
 * Fetches GET /api/org/tree and renders a vertical D3 tree inside
 * #org-tree-panel.  Matches the visual style of dag.js: same PALETTE,
 * same font stack, same link stroke, same zoom behaviour.
 *
 * Node cards show: role name, tier badge, assigned phase chips (future),
 * and the first two compatible-figure avatars from the taxonomy.
 *
 * Clicking a node navigates to /roles/<slug> (the Role Studio editor).
 *
 * D3 is loaded from CDN in the page's {% block head %} — this module
 * guards against d3 being absent so the page degrades gracefully.
 */

// ── Visual constants (mirror dag.js) ─────────────────────────────────────────

const PALETTE = [
  '#3b82f6', '#6366f1', '#14b8a6', '#22c55e',
  '#f97316', '#ef4444', '#a855f7', '#06b6d4', '#eab308', '#ec4899',
];

const TIER_COLORS = {
  'C-Suite': '#6366f1',
  'VP':      '#14b8a6',
  'Worker':  '#3b82f6',
  'org':     '#a855f7',
};

const CARD_W   = 160;
const CARD_H   = 80;
const DX       = 200; // horizontal spacing between nodes
const DY       = 130; // vertical spacing between tree levels

// ── Module state ─────────────────────────────────────────────────────────────

let _svg    = null;
let _g      = null;
let _zoom   = null;
let _width  = 900;
let _height = 520;

// ── Entry point ───────────────────────────────────────────────────────────────

/**
 * Initialise the org chart tree.  Called once the DOM is ready.
 * Fetches /api/org/tree and renders the D3 tree; shows a placeholder
 * when no active preset is selected (HTTP 404 from the endpoint).
 */
export async function initOrgChartTree() {
  if (typeof d3 === 'undefined') {
    _showMessage('D3 library not loaded — tree unavailable.');
    return;
  }

  const panel = document.getElementById('org-tree-panel');
  if (!panel) return;

  _showMessage('Loading org tree…');

  let data;
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
    data = await resp.json();
  } catch (err) {
    _showMessage('Network error loading org tree.');
    return;
  }

  _clearPanel();
  _render(panel, data);
}

// ── Render ────────────────────────────────────────────────────────────────────

function _render(panel, rootData) {
  const rect  = panel.getBoundingClientRect();
  _width  = Math.max(rect.width  || 900, 400);
  _height = Math.max(rect.height || 520, 400);

  // Convert the flat JSON tree into a D3 hierarchy.
  // Each OrgTreeNode may have tier-group children (no slug) or be a leaf role.
  const hierarchy = _buildHierarchy(rootData);
  const root      = d3.hierarchy(hierarchy);

  // Use d3.tree for a vertical (top-down) layout.
  const treeLayout = d3.tree()
    .nodeSize([DX, DY])
    .separation((a, b) => (a.parent === b.parent ? 1.2 : 1.8));

  treeLayout(root);

  // Compute bounding box to center the tree in the panel.
  let x0 = Infinity, x1 = -Infinity;
  root.each(d => {
    if (d.x < x0) x0 = d.x;
    if (d.x > x1) x1 = d.x;
  });
  const treeW = x1 - x0 + CARD_W + 40;

  _svg = d3.select(panel)
    .append('svg')
    .attr('class', 'org-tree-svg')
    .attr('width',  '100%')
    .attr('height', _height)
    .attr('aria-label', 'Org chart tree visualization');

  _zoom = d3.zoom()
    .scaleExtent([0.1, 4])
    .on('zoom', ev => _g.attr('transform', ev.transform));

  _svg.call(_zoom);

  _g = _svg.append('g').attr('class', 'org-tree-container');

  // Arrow marker — same style as dag.js
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

  // ── Links ────────────────────────────────────────────────────────────────
  _g.append('g')
    .attr('class', 'org-tree-links')
    .selectAll('path')
    .data(root.links())
    .join('path')
    .attr('d', d => _elbow(d))
    .attr('fill', 'none')
    .attr('stroke', '#6b7280')
    .attr('stroke-width', 1.5)
    .attr('opacity', 0.6)
    .attr('marker-end', 'url(#org-arrow)');

  // ── Nodes ────────────────────────────────────────────────────────────────
  const nodeGroup = _g.append('g').attr('class', 'org-tree-nodes');

  const node = nodeGroup
    .selectAll('g.org-node')
    .data(root.descendants())
    .join('g')
    .attr('class', 'org-node')
    .attr('transform', d => `translate(${d.x - CARD_W / 2},${d.y - CARD_H / 2})`)
    .style('cursor', d => d.data.slug ? 'pointer' : 'default')
    .on('click', (_ev, d) => {
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

  // Role name (or tier-group label)
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

  // Tier badge (only for role leaf nodes)
  node.filter(d => !!d.data.slug)
    .append('rect')
    .attr('x', 8)
    .attr('y', 30)
    .attr('width', 60)
    .attr('height', 16)
    .attr('rx', 4)
    .attr('fill', d => TIER_COLORS[d.data.tier] || '#6b7280')
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

  // Figure avatar chips (first 2 figures, shown as small colored circles)
  node.filter(d => !!d.data.slug && Array.isArray(d.data.figures) && d.data.figures.length > 0)
    .each(function(d) {
      const g = d3.select(this);
      const figures = d.data.figures.slice(0, 2);
      figures.forEach((fig, i) => {
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

  // Phase chips (assigned_phases — empty for now, reserved for future)
  node.filter(d => !!d.data.slug && Array.isArray(d.data.assigned_phases) && d.data.assigned_phases.length > 0)
    .each(function(d) {
      const g = d3.select(this);
      d.data.assigned_phases.slice(0, 2).forEach((phase, i) => {
        const color = PALETTE[i % PALETTE.length];
        const chipX = 8 + i * 52;
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
          .text(phase.split('/').pop() || phase);
      });
    });

  // Tooltip for role nodes
  const tooltip = document.getElementById('org-tree-tooltip');
  node.filter(d => !!d.data.slug)
    .on('mouseover', (ev, d) => {
      if (!tooltip) return;
      tooltip.innerHTML =
        `<span class="org-tooltip-name">${d.data.name}</span>` +
        `<span class="org-tooltip-tier">${d.data.tier}</span>` +
        (d.data.figures && d.data.figures.length
          ? `<span class="org-tooltip-figs">Figures: ${d.data.figures.join(', ')}</span>`
          : '');
      tooltip.style.display = 'block';
    })
    .on('mousemove', ev => {
      if (!tooltip) return;
      const panelRect = panel.getBoundingClientRect();
      tooltip.style.left = (ev.clientX - panelRect.left + 12) + 'px';
      tooltip.style.top  = (ev.clientY - panelRect.top  + 12) + 'px';
    })
    .on('mouseout', () => {
      if (tooltip) tooltip.style.display = 'none';
    });

  // Initial fit-to-view so the full tree is visible without manual zoom.
  _fitView(root);

  // Re-centre when the panel resizes.
  const ro = new ResizeObserver(() => {
    const r = panel.getBoundingClientRect();
    _width  = Math.max(r.width  || 900, 400);
    _height = Math.max(r.height || 520, 400);
    _svg.attr('height', _height);
    _fitView(root);
  });
  ro.observe(panel);
}

// ── Helpers ───────────────────────────────────────────────────────────────────

/**
 * Convert the OrgTreeNode JSON into a flat list of nodes suitable for
 * d3.hierarchy().  Tier-group children (leadership / workers) become
 * intermediate nodes; roles within them become leaf nodes.
 */
function _buildHierarchy(node) {
  const result = {
    name:    node.name,
    id:      node.id,
    tier:    node.tier,
    slug:    null,
    figures: [],
    assigned_phases: [],
    children: [],
  };

  for (const child of node.children || []) {
    const tierNode = {
      name:    child.name,
      id:      child.id,
      tier:    child.tier,
      slug:    null,
      figures: [],
      assigned_phases: [],
      children: (child.roles || []).map(role => ({
        name:            role.name,
        id:              role.slug,
        slug:            role.slug,
        tier:            role.tier,
        figures:         role.figures || [],
        assigned_phases: role.assigned_phases || [],
        children:        [],
      })),
    };
    result.children.push(tierNode);
  }

  return result;
}

/**
 * Elbow curve connector — top-to-bottom orthogonal path matching the
 * vertical tree layout.  Same visual grammar as dag.js links.
 */
function _elbow(d) {
  const srcX  = d.source.x;
  const srcY  = d.source.y + CARD_H / 2;
  const tgtX  = d.target.x;
  const tgtY  = d.target.y - CARD_H / 2;
  const midY  = (srcY + tgtY) / 2;
  return `M${srcX},${srcY} C${srcX},${midY} ${tgtX},${midY} ${tgtX},${tgtY}`;
}

/** Card fill colour: org root purple, tier groups dark, roles by tier. */
function _cardFill(d) {
  if (d.tier === 'org') return '#4c1d95';
  if (!d.slug) return '#1e293b';
  return TIER_COLORS[d.tier] || '#6b7280';
}

/** Card border — highlight role nodes with a subtle glow stroke. */
function _cardStroke(d) {
  if (d.tier === 'org') return '#7c3aed';
  if (!d.slug) return '#334155';
  return '#64748b';
}

/** Zoom/pan the SVG so the entire tree fits within the visible panel. */
function _fitView(root) {
  if (!_svg || !_g || !_zoom) return;

  const nodes  = root.descendants();
  const pad    = 48;
  const minX   = Math.min(...nodes.map(d => d.x)) - CARD_W / 2;
  const maxX   = Math.max(...nodes.map(d => d.x)) + CARD_W / 2;
  const minY   = Math.min(...nodes.map(d => d.y)) - CARD_H / 2;
  const maxY   = Math.max(...nodes.map(d => d.y)) + CARD_H / 2;
  const bboxW  = Math.max(maxX - minX, 1);
  const bboxH  = Math.max(maxY - minY, 1);
  const scale  = Math.min(
    (_width  - pad * 2) / bboxW,
    (_height - pad * 2) / bboxH,
    2,
  );
  const tx = (_width  / 2) - (minX + bboxW / 2) * scale;
  const ty = pad - minY * scale;

  _svg.transition().duration(400)
    .call(_zoom.transform, d3.zoomIdentity.translate(tx, ty).scale(scale));
}

/** Replace panel contents with a centred message string. */
function _showMessage(msg) {
  const panel = document.getElementById('org-tree-panel');
  if (!panel) return;
  panel.innerHTML = `<p class="org-tree-placeholder">${msg}</p>`;
}

/** Remove all children from the panel before (re-)rendering. */
function _clearPanel() {
  const panel = document.getElementById('org-tree-panel');
  if (panel) panel.innerHTML = '';
}
