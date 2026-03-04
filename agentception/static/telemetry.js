/**
 * telemetry.js — Agentception Telemetry D3 Dashboard
 *
 * Seven D3 v7 render functions, each writing into a fixed container id.
 * Loaded only on /telemetry (not in base.html).
 *
 * Data is read from <script type="application/json"> tags injected by Jinja:
 *   #telemetry-waves-data  — list[WaveSummary.model_dump()]
 *   #telemetry-trend-data  — list[ACPipelineSnapshot row dicts]
 *
 * All functions are exposed on window.telemetry so Alpine's telemetryDash()
 * can call them by tab name.
 */

(function () {
  'use strict';

  // ── Read embedded JSON blobs ─────────────────────────────────────────────

  function readJson(id) {
    const el = document.getElementById(id);
    if (!el) return [];
    try { return JSON.parse(el.textContent); } catch (_) { return []; }
  }

  let _waves = readJson('telemetry-waves-data');
  let _trend = readJson('telemetry-trend-data');

  /** Re-read trend data after HTMX partial swap (called by hx-on::after-swap). */
  function refreshTrendData() {
    _trend = readJson('telemetry-trend-data');
    renderTrend();
  }

  // ── Colour palette (mirrors CSS custom properties) ───────────────────────

  const C = {
    accent:   '#8b5cf6',
    accentLt: '#a78bfa',
    info:     '#06b6d4',
    success:  '#22c55e',
    warn:     '#f59e0b',
    danger:   '#ef4444',
    muted:    'rgba(255,255,255,0.35)',
    grid:     'rgba(255,255,255,0.07)',
    bg:       'rgba(255,255,255,0.03)',
  };

  // Status → colour mapping (agent/wave statuses)
  const STATUS_COLOR = {
    done:           C.success,
    complete:       C.success,
    implementing:   C.accent,
    active:         C.accentLt,
    stale:          C.warn,
    error:          C.danger,
    unknown:        C.muted,
  };

  function statusColor(s) {
    return STATUS_COLOR[(s || 'unknown').toLowerCase()] || C.muted;
  }

  // ── Shared helpers ───────────────────────────────────────────────────────

  /** Create (or clear) a tooltip div inside a container. */
  function makeTooltip(container) {
    let tip = container.querySelector('.d3-tooltip');
    if (!tip) {
      tip = document.createElement('div');
      tip.className = 'd3-tooltip';
      container.style.position = 'relative';
      container.appendChild(tip);
    }
    return tip;
  }

  function showTip(tip, html, event) {
    tip.innerHTML = html;
    tip.classList.add('visible');
    const rect = tip.parentElement.getBoundingClientRect();
    let x = event.clientX - rect.left + 12;
    let y = event.clientY - rect.top  - 10;
    // Keep inside container
    const tw = tip.offsetWidth || 160;
    if (x + tw > rect.width) x = x - tw - 24;
    tip.style.left = x + 'px';
    tip.style.top  = y + 'px';
  }

  function hideTip(tip) {
    tip.classList.remove('visible');
  }

  /** Format a seconds count as "1h 23m" or "45s". */
  function fmtDuration(s) {
    if (!s || s < 0) return '—';
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = Math.floor(s % 60);
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${sec}s`;
    return `${sec}s`;
  }

  /** Truncate a string to n chars with ellipsis. */
  function trunc(str, n) {
    if (!str) return '';
    return str.length > n ? str.slice(0, n) + '…' : str;
  }

  /** Get the container element and its bounding box. */
  function getContainer(id) {
    const el = document.getElementById(id);
    if (!el) return null;
    d3.select(el).selectAll('svg').remove(); // clear previous render
    return el;
  }

  // ── 1. WAVE TIMELINE GANTT ───────────────────────────────────────────────

  function renderGantt() {
    const container = getContainer('chart-main');
    if (!container) return;
    const tip = makeTooltip(container);

    const waves = _waves.filter(w => w.started_at);
    if (!waves.length) {
      container.innerHTML = '<div class="telemetry-empty">No wave data yet.</div>';
      return;
    }

    const W = container.clientWidth  || 640;
    const H = container.clientHeight || 380;
    const margin = { top: 24, right: 20, bottom: 36, left: 20 };
    const iW = W - margin.left - margin.right;
    const iH = H - margin.top  - margin.bottom;

    const minT = d3.min(waves, d => d.started_at);
    const maxT = d3.max(waves, d => d.ended_at || (Date.now() / 1000));
    const xScale = d3.scaleLinear().domain([minT, maxT]).range([0, iW]);

    const rowH  = Math.min(36, iH / waves.length - 6);
    const rowGap = Math.min(8, (iH - rowH * waves.length) / Math.max(waves.length - 1, 1));

    const svg = d3.select(container)
      .append('svg')
      .attr('width', W)
      .attr('height', H);

    const g = svg.append('g')
      .attr('transform', `translate(${margin.left},${margin.top})`);

    // X axis
    const xAxis = d3.axisBottom(xScale)
      .ticks(5)
      .tickFormat(t => {
        const d = new Date(t * 1000);
        return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      });

    g.append('g')
      .attr('class', 'd3-axis')
      .attr('transform', `translate(0,${iH})`)
      .call(xAxis);

    // Grid lines
    g.append('g')
      .attr('class', 'd3-grid')
      .attr('transform', `translate(0,${iH})`)
      .call(d3.axisBottom(xScale).ticks(5).tickSize(-iH).tickFormat(''));

    // Gantt bars
    waves.forEach((w, i) => {
      const y   = i * (rowH + rowGap);
      const x1  = xScale(w.started_at);
      const x2  = xScale(w.ended_at || (Date.now() / 1000));
      const barW = Math.max(x2 - x1, 4);

      const isActive = !w.ended_at;
      const fill = isActive ? C.accentLt : C.accent;

      const bar = g.append('rect')
        .attr('x', x1).attr('y', y)
        .attr('width', barW).attr('height', rowH)
        .attr('rx', 4).attr('ry', 4)
        .attr('fill', fill)
        .attr('opacity', 0.85)
        .style('cursor', 'pointer');

      if (isActive) {
        bar.style('animation', 'bar-pulse 2s ease-in-out infinite');
      }

      // Label inside bar if space allows
      if (barW > 48) {
        g.append('text')
          .attr('x', x1 + 6)
          .attr('y', y + rowH / 2 + 4)
          .attr('font-size', '0.62rem')
          .attr('fill', '#fff')
          .text(trunc(w.batch_id || `Wave ${i + 1}`, 22));
      }

      bar.on('mousemove', (event) => {
          const dur = w.ended_at ? fmtDuration(w.ended_at - w.started_at) : 'active';
          showTip(tip,
            `<span class="d3-tooltip-key">Batch</span>${trunc(w.batch_id || '—', 24)}<br>` +
            `<span class="d3-tooltip-key">Issues</span>${(w.issues_worked || []).length}<br>` +
            `<span class="d3-tooltip-key">Agents</span>${(w.agents || []).length}<br>` +
            `<span class="d3-tooltip-key">Duration</span>${dur}<br>` +
            `<span class="d3-tooltip-key">Cost</span>$${(w.estimated_cost_usd || 0).toFixed(4)}`,
            event);
        })
        .on('mouseleave', () => hideTip(tip));
    });
  }

  // ── 2. CUMULATIVE COST AREA ──────────────────────────────────────────────

  function renderCostArea() {
    const container = getContainer('chart-main');
    if (!container) return;
    const tip = makeTooltip(container);

    const waves = _waves.filter(w => w.started_at).slice().sort((a, b) => a.started_at - b.started_at);
    if (!waves.length) {
      container.innerHTML = '<div class="telemetry-empty">No cost data yet.</div>';
      return;
    }

    const W = container.clientWidth  || 640;
    const H = container.clientHeight || 380;
    const margin = { top: 24, right: 24, bottom: 36, left: 52 };
    const iW = W - margin.left - margin.right;
    const iH = H - margin.top  - margin.bottom;

    let cumCost = 0;
    const points = waves.map(w => {
      cumCost += w.estimated_cost_usd || 0;
      return { t: w.started_at, cost: cumCost, wave: w };
    });

    const xScale = d3.scaleLinear()
      .domain(d3.extent(points, d => d.t))
      .range([0, iW]);
    const yScale = d3.scaleLinear()
      .domain([0, d3.max(points, d => d.cost) * 1.1])
      .range([iH, 0]);

    const svg = d3.select(container).append('svg').attr('width', W).attr('height', H);
    const g = svg.append('g').attr('transform', `translate(${margin.left},${margin.top})`);

    // Grid
    g.append('g').attr('class', 'd3-grid')
      .call(d3.axisLeft(yScale).ticks(5).tickSize(-iW).tickFormat(''));

    // Area
    const area = d3.area()
      .x(d => xScale(d.t))
      .y0(iH)
      .y1(d => yScale(d.cost))
      .curve(d3.curveStepAfter);

    const defs = svg.append('defs');
    const grad = defs.append('linearGradient').attr('id', 'cost-area-grad').attr('gradientUnits', 'userSpaceOnUse')
      .attr('x1', 0).attr('y1', 0).attr('x2', 0).attr('y2', iH + margin.top);
    grad.append('stop').attr('offset', '0%').attr('stop-color', C.accent).attr('stop-opacity', 0.4);
    grad.append('stop').attr('offset', '100%').attr('stop-color', C.accent).attr('stop-opacity', 0.02);

    g.append('path').datum(points)
      .attr('fill', 'url(#cost-area-grad)')
      .attr('d', area);

    // Step line
    const line = d3.line()
      .x(d => xScale(d.t))
      .y(d => yScale(d.cost))
      .curve(d3.curveStepAfter);

    g.append('path').datum(points)
      .attr('fill', 'none')
      .attr('stroke', C.accent)
      .attr('stroke-width', 2)
      .attr('d', line);

    // Dots + tooltips
    g.selectAll('.cost-dot')
      .data(points).join('circle')
      .attr('class', 'cost-dot')
      .attr('cx', d => xScale(d.t))
      .attr('cy', d => yScale(d.cost))
      .attr('r', 4)
      .attr('fill', C.accent)
      .attr('stroke', '#111')
      .attr('stroke-width', 1.5)
      .style('cursor', 'pointer')
      .on('mousemove', (event, d) => {
        showTip(tip,
          `<span class="d3-tooltip-key">Cumulative</span>$${d.cost.toFixed(4)}<br>` +
          `<span class="d3-tooltip-key">Wave cost</span>$${(d.wave.estimated_cost_usd || 0).toFixed(4)}<br>` +
          `<span class="d3-tooltip-key">Batch</span>${trunc(d.wave.batch_id || '—', 20)}`,
          event);
      })
      .on('mouseleave', () => hideTip(tip));

    // Axes
    g.append('g').attr('class', 'd3-axis').attr('transform', `translate(0,${iH})`)
      .call(d3.axisBottom(xScale).ticks(5)
        .tickFormat(t => new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })));

    g.append('g').attr('class', 'd3-axis')
      .call(d3.axisLeft(yScale).ticks(5).tickFormat(d => `$${d.toFixed(3)}`));
  }

  // ── 3. PIPELINE TREND MULTI-LINE ─────────────────────────────────────────

  function renderTrend() {
    const container = getContainer('chart-main');
    if (!container) return;
    const tip = makeTooltip(container);

    if (!_trend.length) {
      container.innerHTML = '<div class="telemetry-empty">No trend snapshots yet. Check back after the first pipeline poll.</div>';
      return;
    }

    const W = container.clientWidth  || 640;
    const H = container.clientHeight || 380;
    const margin = { top: 24, right: 80, bottom: 36, left: 48 };
    const iW = W - margin.left - margin.right;
    const iH = H - margin.top  - margin.bottom;

    const parseDate = d => new Date(d.polled_at);
    const xScale = d3.scaleTime()
      .domain(d3.extent(_trend, parseDate))
      .range([0, iW]);

    const maxY = d3.max(_trend, d =>
      Math.max(+d.issues_open || 0, +d.prs_open || 0, +d.agents_active || 0, +d.alert_count || 0)
    );
    const yScale = d3.scaleLinear().domain([0, maxY * 1.1 || 1]).range([iH, 0]);

    const series = [
      { key: 'issues_open',   label: 'Issues Open',   color: C.accent },
      { key: 'prs_open',      label: 'PRs Open',      color: C.info   },
      { key: 'agents_active', label: 'Agents Active', color: C.success },
      { key: 'alert_count',   label: 'Alerts',        color: C.warn   },
    ];

    const svg = d3.select(container).append('svg').attr('width', W).attr('height', H);
    const g = svg.append('g').attr('transform', `translate(${margin.left},${margin.top})`);

    // Grid
    g.append('g').attr('class', 'd3-grid')
      .call(d3.axisLeft(yScale).ticks(5).tickSize(-iW).tickFormat(''));

    // Lines
    const lineGen = d3.line().x(d => xScale(parseDate(d))).y(d => yScale(+d || 0)).curve(d3.curveCatmullRom);

    series.forEach(({ key, color }) => {
      g.append('path')
        .datum(_trend)
        .attr('fill', 'none')
        .attr('stroke', color)
        .attr('stroke-width', 1.8)
        .attr('d', lineGen.y(d => yScale(+(d[key]) || 0)));
    });

    // Crosshair + focus dots
    const crosshair = g.append('line').attr('class', 'd3-crosshair').attr('y1', 0).attr('y2', iH).attr('opacity', 0);
    const focusDots = series.map(({ color }) =>
      g.append('circle').attr('r', 4).attr('fill', color).attr('stroke', '#111').attr('stroke-width', 1).attr('opacity', 0)
    );

    // Invisible overlay for hover
    g.append('rect')
      .attr('width', iW).attr('height', iH)
      .attr('fill', 'none').attr('pointer-events', 'all')
      .on('mousemove', (event) => {
        const [mx] = d3.pointer(event);
        const bisect = d3.bisector(parseDate).left;
        const t0 = xScale.invert(mx);
        const idx = Math.max(0, Math.min(_trend.length - 1, bisect(_trend, t0)));
        const d = _trend[idx];
        const x = xScale(parseDate(d));

        crosshair.attr('x1', x).attr('x2', x).attr('opacity', 0.6);
        series.forEach(({ key }, i) => {
          focusDots[i].attr('cx', x).attr('cy', yScale(+(d[key]) || 0)).attr('opacity', 1);
        });

        const time = parseDate(d).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        showTip(tip,
          `<span class="d3-tooltip-key">Time</span>${time}<br>` +
          series.map(({ key, label, color }) =>
            `<span style="color:${color}">${label}</span> ${+(d[key]) || 0}`
          ).join('<br>'),
          event);
      })
      .on('mouseleave', () => {
        crosshair.attr('opacity', 0);
        focusDots.forEach(dot => dot.attr('opacity', 0));
        hideTip(tip);
      });

    // Axes
    g.append('g').attr('class', 'd3-axis').attr('transform', `translate(0,${iH})`)
      .call(d3.axisBottom(xScale).ticks(5).tickFormat(d => d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })));
    g.append('g').attr('class', 'd3-axis').call(d3.axisLeft(yScale).ticks(5));

    // Legend
    const legend = g.append('g').attr('transform', `translate(${iW + 8}, 0)`);
    series.forEach(({ label, color }, i) => {
      const ly = i * 20;
      legend.append('rect').attr('x', 0).attr('y', ly).attr('width', 10).attr('height', 10).attr('rx', 2).attr('fill', color);
      legend.append('text').attr('x', 14).attr('y', ly + 9).attr('font-size', '0.65rem').attr('fill', C.muted).text(label);
    });
  }

  // ── 4. AGENT ROLE DONUT ──────────────────────────────────────────────────

  /**
   * Render the role donut into a given container id.
   * Tab renders go to 'chart-main'; sidebar renders go to 'chart-donut'.
   */
  function renderDonut(targetId) {
    const container = getContainer(targetId || 'chart-main');
    if (!container) return;
    const tip = makeTooltip(container);

    // Aggregate role counts from all waves
    const roleCounts = {};
    _waves.forEach(w => {
      (w.agents || []).forEach(a => {
        roleCounts[a.role] = (roleCounts[a.role] || 0) + 1;
      });
    });

    const entries = Object.entries(roleCounts).sort((a, b) => b[1] - a[1]);
    if (!entries.length) {
      container.innerHTML = '<div class="telemetry-empty">No agents yet.</div>';
      return;
    }

    const W = container.clientWidth  || 300;
    const H = container.clientHeight || 180;
    const r  = Math.min(W, H) / 2 - 12;
    const ir = r * 0.52; // inner radius (donut hole)

    const color = d3.scaleOrdinal()
      .domain(entries.map(e => e[0]))
      .range(d3.schemeTableau10);

    const pie = d3.pie().value(d => d[1]).sort(null);
    const arc = d3.arc().innerRadius(ir).outerRadius(r);
    const arcHover = d3.arc().innerRadius(ir).outerRadius(r + 6);

    const svg = d3.select(container).append('svg').attr('width', W).attr('height', H);
    const g = svg.append('g').attr('transform', `translate(${Math.min(W * 0.45, r + 12)},${H / 2})`);

    const slices = g.selectAll('path').data(pie(entries)).join('path')
      .attr('d', arc)
      .attr('fill', d => color(d.data[0]))
      .attr('stroke', '#111')
      .attr('stroke-width', 1)
      .style('cursor', 'pointer')
      .on('mousemove', (event, d) => {
        d3.select(event.currentTarget).attr('d', arcHover);
        showTip(tip,
          `<span class="d3-tooltip-key">Role</span>${d.data[0]}<br>` +
          `<span class="d3-tooltip-key">Agents</span>${d.data[1]}`,
          event);
      })
      .on('mouseleave', (event) => {
        d3.select(event.currentTarget).attr('d', arc);
        hideTip(tip);
      });

    // Centre label — total agents
    const total = entries.reduce((s, e) => s + e[1], 0);
    g.append('text').attr('text-anchor', 'middle').attr('dy', '-0.2em')
      .attr('font-size', '1.3rem').attr('font-weight', '700').attr('fill', '#fff')
      .text(total);
    g.append('text').attr('text-anchor', 'middle').attr('dy', '1.1em')
      .attr('font-size', '0.6rem').attr('fill', C.muted).text('agents');

    // Legend (right side)
    const legendX = Math.min(W * 0.45, r + 12) + r + 16;
    const legendG = svg.append('g').attr('transform', `translate(${legendX}, ${(H - entries.slice(0, 8).length * 18) / 2})`);
    entries.slice(0, 8).forEach(([role, count], i) => {
      legendG.append('rect').attr('x', 0).attr('y', i * 18).attr('width', 8).attr('height', 8).attr('rx', 2).attr('fill', color(role));
      legendG.append('text').attr('x', 12).attr('y', i * 18 + 8)
        .attr('font-size', '0.6rem').attr('fill', C.muted)
        .text(`${trunc(role, 16)} (${count})`);
    });
  }

  // ── 5. WAVE PERFORMANCE SCATTER ──────────────────────────────────────────

  function renderScatter() {
    const container = getContainer('chart-main');
    if (!container) return;
    const tip = makeTooltip(container);

    const waves = _waves.filter(w => w.started_at && w.ended_at);
    if (!waves.length) {
      container.innerHTML = '<div class="telemetry-empty">No completed waves yet.</div>';
      return;
    }

    const W = container.clientWidth  || 300;
    const H = container.clientHeight || 180;
    const margin = { top: 16, right: 12, bottom: 32, left: 40 };
    const iW = W - margin.left - margin.right;
    const iH = H - margin.top  - margin.bottom;

    const maxIssues = d3.max(waves, w => (w.issues_worked || []).length) || 1;

    const xScale = d3.scaleLinear()
      .domain(d3.extent(waves, w => w.started_at))
      .range([0, iW]).nice();
    const yScale = d3.scaleLinear()
      .domain([0, d3.max(waves, w => w.ended_at - w.started_at) * 1.1 || 1])
      .range([iH, 0]);
    const rScale = d3.scaleSqrt()
      .domain([0, maxIssues])
      .range([4, 16]);

    const svg = d3.select(container).append('svg').attr('width', W).attr('height', H);
    const g = svg.append('g').attr('transform', `translate(${margin.left},${margin.top})`);

    g.append('g').attr('class', 'd3-grid')
      .call(d3.axisLeft(yScale).ticks(4).tickSize(-iW).tickFormat(''));

    g.selectAll('circle').data(waves).join('circle')
      .attr('cx', w => xScale(w.started_at))
      .attr('cy', w => yScale(w.ended_at - w.started_at))
      .attr('r',  w => rScale((w.issues_worked || []).length))
      .attr('fill', C.accent).attr('opacity', 0.7)
      .attr('stroke', '#111').attr('stroke-width', 1)
      .style('cursor', 'pointer')
      .on('mousemove', (event, w) => {
        showTip(tip,
          `<span class="d3-tooltip-key">Duration</span>${fmtDuration(w.ended_at - w.started_at)}<br>` +
          `<span class="d3-tooltip-key">Issues</span>${(w.issues_worked || []).length}<br>` +
          `<span class="d3-tooltip-key">Agents</span>${(w.agents || []).length}`,
          event);
      })
      .on('mouseleave', () => hideTip(tip));

    g.append('g').attr('class', 'd3-axis').attr('transform', `translate(0,${iH})`)
      .call(d3.axisBottom(xScale).ticks(4)
        .tickFormat(t => new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })));
    g.append('g').attr('class', 'd3-axis')
      .call(d3.axisLeft(yScale).ticks(4).tickFormat(t => fmtDuration(t)));
  }

  // ── 6. AGENT STATUS STACKED BAR ──────────────────────────────────────────

  function renderStackedBar(targetId) {
    const container = getContainer(targetId || 'chart-main');
    if (!container) return;
    const tip = makeTooltip(container);

    // Take last 10 waves that have agents
    const waves = _waves.filter(w => (w.agents || []).length).slice(-10);
    if (!waves.length) {
      container.innerHTML = '<div class="telemetry-empty">No agent data yet.</div>';
      return;
    }

    const statusKeys = ['done', 'implementing', 'stale', 'error', 'unknown'];

    // Build stack data
    const data = waves.map((w, i) => {
      const row = { i, label: trunc(w.batch_id || `Wave ${i}`, 12) };
      const agents = w.agents || [];
      statusKeys.forEach(k => {
        row[k] = agents.filter(a => (a.status || 'unknown').toLowerCase() === k).length;
      });
      row['other'] = agents.length - statusKeys.reduce((s, k) => s + row[k], 0);
      return row;
    });
    const allKeys = [...statusKeys, 'other'];

    const W = container.clientWidth  || 300;
    const H = container.clientHeight || 180;
    const margin = { top: 12, right: 12, bottom: 48, left: 32 };
    const iW = W - margin.left - margin.right;
    const iH = H - margin.top  - margin.bottom;

    const xScale = d3.scaleBand().domain(data.map(d => d.i)).range([0, iW]).padding(0.25);
    const maxAgents = d3.max(data, d => allKeys.reduce((s, k) => s + (d[k] || 0), 0)) || 1;
    const yScale = d3.scaleLinear().domain([0, maxAgents]).range([iH, 0]);

    const colorMap = { done: C.success, implementing: C.accent, stale: C.warn, error: C.danger, unknown: C.muted, other: C.muted };

    const svg = d3.select(container).append('svg').attr('width', W).attr('height', H);
    const g = svg.append('g').attr('transform', `translate(${margin.left},${margin.top})`);

    g.append('g').attr('class', 'd3-grid')
      .call(d3.axisLeft(yScale).ticks(4).tickSize(-iW).tickFormat(''));

    const stack = d3.stack().keys(allKeys)(data);

    stack.forEach(layer => {
      const key = layer.key;
      g.selectAll(`.bar-${key}`).data(layer).join('rect')
        .attr('class', `bar-${key}`)
        .attr('x',      d => xScale(d.data.i))
        .attr('y',      d => yScale(d[1]))
        .attr('height', d => Math.max(0, yScale(d[0]) - yScale(d[1])))
        .attr('width',  xScale.bandwidth())
        .attr('fill',   colorMap[key] || C.muted)
        .on('mousemove', (event, d) => {
          showTip(tip,
            `<span class="d3-tooltip-key">Wave</span>${d.data.label}<br>` +
            `<span class="d3-tooltip-key">${key}</span>${d[1] - d[0]}`,
            event);
        })
        .on('mouseleave', () => hideTip(tip));
    });

    g.append('g').attr('class', 'd3-axis').attr('transform', `translate(0,${iH})`)
      .call(d3.axisBottom(xScale).tickFormat(i => data[i]?.label || ''))
      .selectAll('text').attr('transform', 'rotate(-30)').attr('text-anchor', 'end');
    g.append('g').attr('class', 'd3-axis').call(d3.axisLeft(yScale).ticks(4));
  }

  // ── 7. MESSAGE COUNT HISTOGRAM ───────────────────────────────────────────

  function renderHistogram(targetId) {
    const container = getContainer(targetId || 'chart-main');
    if (!container) return;
    const tip = makeTooltip(container);

    const msgCounts = _waves.flatMap(w => (w.agents || []).map(a => +(a.message_count) || 0));
    if (!msgCounts.length) {
      container.innerHTML = '<div class="telemetry-empty">No agent message data yet.</div>';
      return;
    }

    const W = container.clientWidth  || 300;
    const H = container.clientHeight || 180;
    const margin = { top: 12, right: 12, bottom: 32, left: 36 };
    const iW = W - margin.left - margin.right;
    const iH = H - margin.top  - margin.bottom;

    const xScale = d3.scaleLinear().domain([0, d3.max(msgCounts)]).range([0, iW]).nice();
    const bins = d3.bin().domain(xScale.domain()).thresholds(12)(msgCounts);
    const yScale = d3.scaleLinear().domain([0, d3.max(bins, b => b.length)]).range([iH, 0]);

    const svg = d3.select(container).append('svg').attr('width', W).attr('height', H);
    const g = svg.append('g').attr('transform', `translate(${margin.left},${margin.top})`);

    g.append('g').attr('class', 'd3-grid')
      .call(d3.axisLeft(yScale).ticks(4).tickSize(-iW).tickFormat(''));

    g.selectAll('rect').data(bins).join('rect')
      .attr('x',      b => xScale(b.x0) + 1)
      .attr('y',      b => yScale(b.length))
      .attr('width',  b => Math.max(0, xScale(b.x1) - xScale(b.x0) - 2))
      .attr('height', b => iH - yScale(b.length))
      .attr('fill',   C.info).attr('opacity', 0.8)
      .on('mousemove', (event, b) => {
        showTip(tip,
          `<span class="d3-tooltip-key">Messages</span>${b.x0}–${b.x1}<br>` +
          `<span class="d3-tooltip-key">Agents</span>${b.length}`,
          event);
      })
      .on('mouseleave', () => hideTip(tip));

    g.append('g').attr('class', 'd3-axis').attr('transform', `translate(0,${iH})`)
      .call(d3.axisBottom(xScale).ticks(6));
    g.append('g').attr('class', 'd3-axis').call(d3.axisLeft(yScale).ticks(4));
  }

  // ── Public API ───────────────────────────────────────────────────────────

  window.telemetry = {
    renderGantt,
    renderCostArea,
    renderTrend,
    renderDonut,
    renderScatter,
    renderStackedBar,
    renderHistogram,
    refreshTrendData,
  };

})();
