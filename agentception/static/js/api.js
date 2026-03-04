'use strict';

/**
 * Powers each endpoint card on /api.
 *
 * Data is read from data-* attributes on the root element so all static
 * values (method, path template, param specs, body field definitions) live
 * in Python/Jinja rather than in this file.
 *
 * data-method        — HTTP verb (GET, POST, …)
 * data-path          — path template, e.g. /agents/{agent_id}
 * data-query-params  — JSON array of {name, required, description} objects
 * data-body-fields   — JSON array of {name, type, required, default} objects
 */
export function apiEndpoint() {
  return {
    open: false,
    tryOpen: false,
    method: 'GET',
    pathTemplate: '/',
    pathParamNames: [],   // e.g. ['agent_id']
    pathParamValues: {},  // {agent_id: ''}
    queryParams: [],      // [{name, required, description}]
    queryValues: {},      // {name: ''}
    bodyFields: [],
    bodyJson: '',
    response: null,
    sending: false,

    init() {
      this.method       = this.$el.dataset.method || 'GET';
      this.pathTemplate = this.$el.dataset.path   || '/';

      // Query params
      try { this.queryParams = JSON.parse(this.$el.dataset.queryParams || '[]'); }
      catch { this.queryParams = []; }
      for (const p of this.queryParams) this.queryValues[p.name] = '';

      // Body fields → JSON skeleton
      try { this.bodyFields = JSON.parse(this.$el.dataset.bodyFields || '[]'); }
      catch { this.bodyFields = []; }
      if (this.bodyFields.length) {
        const skeleton = {};
        for (const f of this.bodyFields) {
          if (f.default !== null && f.default !== undefined && f.default !== '') {
            skeleton[f.name] = f.default;
          } else if (f.type === 'integer' || f.type === 'number') {
            skeleton[f.name] = 0;
          } else if (f.type === 'boolean') {
            skeleton[f.name] = false;
          } else if (f.type && f.type.startsWith('array')) {
            skeleton[f.name] = [];
          } else {
            skeleton[f.name] = '';
          }
        }
        this.bodyJson = JSON.stringify(skeleton, null, 2);
      }

      // Path params — extract {name} tokens from template
      const matches = this.pathTemplate.match(/\{(\w+)\}/g) || [];
      this.pathParamNames = matches.map(m => m.slice(1, -1));
      for (const n of this.pathParamNames) this.pathParamValues[n] = '';
    },

    get resolvedPath() {
      let url = this.pathTemplate;
      for (const [k, v] of Object.entries(this.pathParamValues)) {
        url = url.replace(`{${k}}`, v ? encodeURIComponent(v) : `{${k}}`);
      }
      const qs = Object.entries(this.queryValues)
        .filter(([, v]) => v !== '')
        .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`);
      if (qs.length) url += '?' + qs.join('&');
      return url;
    },

    // Build a curl command that mirrors the current form state.
    get curlCommand() {
      const base = window.location.origin;
      let cmd = `curl -X ${this.method} '${base}${this.resolvedPath}'`;
      const hasBody = !['GET', 'HEAD'].includes(this.method) && this.bodyJson.trim();
      if (hasBody) {
        // Escape single quotes inside the JSON for a POSIX shell literal.
        const escaped = this.bodyJson.replace(/'/g, "'\\''");
        cmd += ` \\\n  -H 'Content-Type: application/json' \\\n  -d '${escaped}'`;
      }
      return cmd;
    },

    formatSize(bytes) {
      if (bytes < 1024) return `${bytes} B`;
      if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
      return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    },

    async send() {
      this.sending = true;
      this.response = null;
      const t0 = performance.now();
      try {
        const opts = { method: this.method, headers: {} };
        if (!['GET', 'HEAD', 'DELETE'].includes(this.method) && this.bodyJson.trim()) {
          opts.headers['Content-Type'] = 'application/json';
          opts.body = this.bodyJson;
        }
        const resp = await fetch(this.resolvedPath, opts);
        const elapsed = Math.round(performance.now() - t0);
        const text = await resp.text();

        // Collect all response headers.
        const headers = [];
        resp.headers.forEach((value, name) => headers.push({ name, value }));
        const contentType = resp.headers.get('content-type') || '';
        const size = new TextEncoder().encode(text).length;

        // Pretty-print JSON bodies; leave everything else as-is.
        let body = text;
        try { body = JSON.stringify(JSON.parse(text), null, 2); } catch { /* not JSON */ }

        this.response = { status: resp.status, ok: resp.ok, body, elapsed, size, contentType, headers, curl: this.curlCommand };
      } catch (err) {
        const elapsed = Math.round(performance.now() - t0);
        this.response = { status: 0, ok: false, body: String(err), elapsed, size: 0, contentType: '', headers: [], curl: this.curlCommand };
      } finally {
        this.sending = false;
      }
    },
  };
}
