'use strict';

/**
 * Drives the Dependency DAG page.  Reads node/edge data from a JSON
 * <script type="application/json"> tag injected by Jinja2, then delegates
 * all rendering to D3.  Alpine manages the thin UI state layer:
 * filter, search, selected-node sidebar, and zoom controls.
 *
 * D3 is loaded from CDN in {% block head %} of dag.html — this function
 * guards against D3 not being present.
 */
export function dagVisualization() {
  const PALETTE = [
    '#3b82f6', '#6366f1', '#14b8a6', '#22c55e',
    '#f97316', '#ef4444', '#a855f7', '#06b6d4', '#eab308', '#ec4899',
  ];
  const DEFAULT_COLOR = '#6b7280';
  const NODE_RADIUS   = 20;

  return {
    // ── State ──────────────────────────────────────────────────────────────
    rawNodes: [],
    rawEdges: [],
    phaseLabels: [],
    phaseColors: {},
    ghBaseUrl: '',

    filter: 'all',
    search: '',
    selectedNode: null,
    refreshing: false,

    // D3 internals — not reactive (managed directly)
    _sim: null,
    _zoom: null,
    _svg: null,
    _container: null,
    _linkGroup: null,
    _nodeGroup: null,

    // ── Computed ───────────────────────────────────────────────────────────
    get filteredNodes() {
      let nodes = this.rawNodes;
      if (this.filter !== 'all') {
        nodes = nodes.filter(n => Array.isArray(n.labels) && n.labels.includes(this.filter));
      }
      if (this.search.trim()) {
        const q = this.search.trim().toLowerCase().replace(/^#/, '');
        nodes = nodes.filter(n =>
          String(n.number).includes(q) ||
          (n.title || '').toLowerCase().includes(q)
        );
      }
      return nodes;
    },

    get filteredEdges() {
      const ids = new Set(this.filteredNodes.map(n => n.number));
      return this.rawEdges.filter(([s, t]) => ids.has(s) && ids.has(t));
    },

    get sidebarOpen() {
      return this.selectedNode !== null;
    },

    // ── Lifecycle ──────────────────────────────────────────────────────────
    init() {
      // Read injected JSON
      const dataEl = document.getElementById('dag-data');
      if (dataEl) {
        try {
          const parsed = JSON.parse(dataEl.textContent);
          this.rawNodes    = parsed.nodes  || [];
          this.rawEdges    = parsed.edges  || [];
          this.phaseLabels = parsed.phase_labels || [];
          this.ghBaseUrl   = parsed.gh_base_url  || '';
        } catch (_) { /* data missing — graph stays empty */ }
      }

      // Build phase → colour map
      this.phaseColors = Object.fromEntries(
        this.phaseLabels.map((lbl, i) => [lbl, PALETTE[i % PALETTE.length]])
      );

      // Boot D3 once DOM is ready
      this.$nextTick(() => this._initD3());
    },

    // ── D3 bootstrap ──────────────────────────────────────────────────────
    _initD3() {
      if (typeof d3 === 'undefined') return;

      // Capture Alpine component reference so non-Alpine closures (e.g.
      // ResizeObserver) can call methods without relying on `this` binding.
      const self = this;

      // Signal latch: mirrors this.filter so the ResizeObserver closure
      // always holds the latest filter value for observability; _render()
      // reads filter state via Alpine's reactive filteredNodes/filteredEdges.
      let _activeFilter = this.filter;

      // Debounce timer for ResizeObserver — acts as a 100 ms low-pass filter
      // on the resize signal so rapid dimension changes coalesce into one
      // _render() call.
      let _resizeTimer = null;

      this._svg = d3.select('#dag-svg');

      this._zoom = d3.zoom()
        .scaleExtent([0.15, 5])
        .on('zoom', (ev) => {
          this._container.attr('transform', ev.transform);
        });

      this._svg.call(this._zoom);

      this._container  = this._svg.append('g').attr('class', 'dag-container');
      this._linkGroup  = this._container.append('g').attr('class', 'dag-links');
      this._nodeGroup  = this._container.append('g').attr('class', 'dag-nodes');

      // Arrow marker
      this._svg.append('defs').append('marker')
        .attr('id', 'dag-arrow')
        .attr('viewBox', '0 0 10 10')
        .attr('refX', 26)
        .attr('refY', 5)
        .attr('markerWidth', 6)
        .attr('markerHeight', 6)
        .attr('orient', 'auto-start-reverse')
        .append('path')
        .attr('d', 'M 0 0 L 10 5 L 0 10 z')
        .attr('fill', '#6b7280');

      this._render();

      // Re-render whenever filter or search changes; keep _activeFilter in sync
      // so the ResizeObserver latch is never stale.
      this.$watch('filter', () => {
        _activeFilter = self.filter;
        self._render();
      });
      this.$watch('search', () => self._render());

      // ResizeObserver — re-centres the force simulation when the SVG element
      // changes dimensions.  The 100 ms debounce is a low-pass filter on the
      // resize signal: rapid resize events coalesce into a single _render()
      // invocation, avoiding mid-drag jitter.
      const _observer = new ResizeObserver(() => {
        clearTimeout(_resizeTimer);
        _resizeTimer = setTimeout(() => self._render(), 100);
      });
      const svgEl = document.getElementById('dag-svg');
      if (svgEl) _observer.observe(svgEl);

      // Teardown: Alpine.js v3's data-object API does not expose a destroy()
      // lifecycle hook on the component itself.  The correct approach is to
      // register an 'alpine:destroy' listener on the root element via x-on or
      // Alpine.bind — that handler should call _observer.disconnect().
      // Leaving it disconnected here means the observer lives until the page
      // is unloaded, which is acceptable for a single-page DAG view.
    },

    // ── D3 render ─────────────────────────────────────────────────────────
    _nodeColor(node) {
      if (!Array.isArray(node.labels)) return DEFAULT_COLOR;
      for (const lbl of node.labels) {
        if (this.phaseColors[lbl]) return this.phaseColors[lbl];
      }
      return DEFAULT_COLOR;
    },

    _render() {
      if (!this._svg || typeof d3 === 'undefined') return;

      const nodes = this.filteredNodes;
      const edges = this.filteredEdges;
      const self  = this;

      if (this._sim) this._sim.stop();

      const svgEl = document.getElementById('dag-svg');
      const W = svgEl ? (svgEl.clientWidth  || 900) : 900;
      const H = svgEl ? (svgEl.clientHeight || 520) : 520;

      const nodeData = nodes.map(n => ({ ...n, id: String(n.number) }));
      const edgeData = edges.map(([s, t]) => ({
        source: String(s),
        target: String(t),
      }));

      this._sim = d3.forceSimulation(nodeData)
        .force('link', d3.forceLink(edgeData).id(d => d.id).distance(110).strength(0.5))
        .force('charge', d3.forceManyBody().strength(-380))
        .force('center', d3.forceCenter(W / 2, H / 2))
        .force('collision', d3.forceCollide(NODE_RADIUS + 8));

      // ── Links ────────────────────────────────────────────────────────────
      const link = this._linkGroup
        .selectAll('line')
        .data(edgeData, d => `${d.source}-${d.target}`)
        .join(
          enter => enter.append('line')
            .attr('stroke', '#6b7280')
            .attr('stroke-width', 1.5)
            .attr('marker-end', 'url(#dag-arrow)')
            .attr('opacity', 0.65),
          update => update,
          exit   => exit.remove(),
        );

      // ── Nodes ────────────────────────────────────────────────────────────
      const tooltip = document.getElementById('dag-tooltip');

      const node = this._nodeGroup
        .selectAll('g.dag-node')
        .data(nodeData, d => d.id)
        .join(
          enter => {
            const g = enter.append('g')
              .attr('class', 'dag-node')
              .style('cursor', 'pointer')
              .call(d3.drag()
                .on('start', (ev, d) => {
                  if (!ev.active) self._sim.alphaTarget(0.3).restart();
                  d.fx = d.x;
                  d.fy = d.y;
                })
                .on('drag', (ev, d) => {
                  d.fx = ev.x;
                  d.fy = ev.y;
                })
                .on('end', (ev, d) => {
                  if (!ev.active) self._sim.alphaTarget(0);
                  d.fx = null;
                  d.fy = null;
                })
              );

            g.append('circle').attr('r', NODE_RADIUS);

            g.append('text')
              .attr('text-anchor', 'middle')
              .attr('dominant-baseline', 'central')
              .attr('font-size', '9px')
              .attr('font-family', 'monospace')
              .attr('fill', '#fff')
              .attr('pointer-events', 'none')
              .text(d => `#${d.number}`);

            return g;
          },
          update => update,
          exit   => exit.remove(),
        );

      // Apply live styles to all nodes
      this._nodeGroup.selectAll('g.dag-node').each(function(d) {
        const g      = d3.select(this);
        const isClosed = String(d.state || '').toUpperCase() === 'CLOSED';
        const isMatch  = self.search.trim()
          ? String(d.number).includes(self.search.replace(/^#/, '')) ||
            (d.title || '').toLowerCase().includes(self.search.toLowerCase())
          : true;

        g.select('circle')
          .attr('fill',         self._nodeColor(d))
          .attr('opacity',      isClosed ? 0.35 : isMatch ? 1.0 : 0.25)
          .attr('stroke',       d.has_wip ? '#22c55e' : 'none')
          .attr('stroke-width', d.has_wip ? 3 : 0);
      });

      // Click → select node (show sidebar)
      this._nodeGroup.selectAll('g.dag-node')
        .on('click', (_ev, d) => {
          self.selectNode(d);
        })
        .on('mouseover', (ev, d) => {
          if (!tooltip) return;
          const blocking = d.blocking_count || 0;
          tooltip.innerHTML =
            `<span class="dag-tooltip-num">#${d.number}</span>` +
            `<span class="dag-tooltip-title">${d.title || ''}</span>` +
            `<div class="dag-tooltip-meta">` +
            (d.has_wip ? `<span class="dag-tooltip-badge wip">WIP</span>` : '') +
            (blocking > 0 ? `<span class="dag-tooltip-badge blocking">blocks ${blocking}</span>` : '') +
            `<span class="dag-tooltip-badge">depth ${d.depth || 0}</span>` +
            `</div>`;
          tooltip.style.display = 'block';
        })
        .on('mousemove', (ev) => {
          if (!tooltip) return;
          const wrapper = document.querySelector('.dag-canvas-wrapper');
          const rect    = wrapper ? wrapper.getBoundingClientRect() : { left: 0, top: 0 };
          tooltip.style.left = (ev.clientX - rect.left + 14) + 'px';
          tooltip.style.top  = (ev.clientY - rect.top  + 14) + 'px';
        })
        .on('mouseout', () => {
          if (tooltip) tooltip.style.display = 'none';
        });

      // Tick
      this._sim.on('tick', () => {
        link
          .attr('x1', d => d.source.x)
          .attr('y1', d => d.source.y)
          .attr('x2', d => d.target.x)
          .attr('y2', d => d.target.y);

        this._nodeGroup.selectAll('g.dag-node')
          .attr('transform', d => `translate(${d.x},${d.y})`);
      });
    },

    // ── Interaction helpers ────────────────────────────────────────────────
    selectNode(nodeOrNum) {
      if (typeof nodeOrNum === 'number' || typeof nodeOrNum === 'string') {
        const num = parseInt(String(nodeOrNum).replace('#', ''), 10);
        nodeOrNum = this.rawNodes.find(n => n.number === num) || null;
      }
      this.selectedNode = nodeOrNum || null;
    },

    closeNode() {
      this.selectedNode = null;
    },

    setFilter(val) {
      this.filter = val;
    },

    zoomIn() {
      if (!this._svg || !this._zoom) return;
      this._svg.transition().duration(250).call(this._zoom.scaleBy, 1.4);
    },

    zoomOut() {
      if (!this._svg || !this._zoom) return;
      this._svg.transition().duration(250).call(this._zoom.scaleBy, 1 / 1.4);
    },

    resetView() {
      if (!this._svg || !this._zoom) return;
      this._svg.transition().duration(300).call(this._zoom.transform, d3.zoomIdentity);
      this._render();
    },

    fitView() {
      if (!this._svg || !this._zoom || !this._sim) return;
      const nodes = this._sim.nodes();
      if (!nodes.length) return;

      const svgEl = document.getElementById('dag-svg');
      const W = svgEl ? (svgEl.clientWidth  || 900) : 900;
      const H = svgEl ? (svgEl.clientHeight || 520) : 520;
      const pad = 48;

      let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
      for (const n of nodes) {
        x0 = Math.min(x0, (n.x || 0) - NODE_RADIUS);
        x1 = Math.max(x1, (n.x || 0) + NODE_RADIUS);
        y0 = Math.min(y0, (n.y || 0) - NODE_RADIUS);
        y1 = Math.max(y1, (n.y || 0) + NODE_RADIUS);
      }

      const bboxW = Math.max(x1 - x0, 1);
      const bboxH = Math.max(y1 - y0, 1);
      const scale = Math.min(
        (W - pad * 2) / bboxW,
        (H - pad * 2) / bboxH,
        5,
      );
      const tx = (W - bboxW * scale) / 2 - x0 * scale;
      const ty = (H - bboxH * scale) / 2 - y0 * scale;

      this._svg.transition().duration(400)
        .call(this._zoom.transform, d3.zoomIdentity.translate(tx, ty).scale(scale));
    },

    async refreshData() {
      this.refreshing = true;
      try {
        const r = await fetch('/api/dag');
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const data       = await r.json();
        this.rawNodes    = data.nodes || [];
        this.rawEdges    = data.edges || [];
        this._render();
      } catch (_) { /* silently fail — stale data stays */ }
      finally { this.refreshing = false; }
    },

    // ── Sidebar dep navigation ─────────────────────────────────────────────
    nodeByNum(num) {
      return this.rawNodes.find(n => n.number === num) || null;
    },

    depTitle(num) {
      const n = this.nodeByNum(num);
      return n ? n.title : `#${num}`;
    },
  };
}
