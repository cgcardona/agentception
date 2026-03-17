/**
 * mcp_session.ts — AgentCeption dashboard MCP client.
 *
 * Implements the client side of the MCP 2025-11-25 Streamable HTTP transport
 * so the Mission Control dashboard can receive server-initiated JSON-RPC
 * requests (elicitation/create) and dispatch responses back to the server.
 *
 * Lifecycle
 * ---------
 * 1. Call ``initMcpSession()`` on page load.
 * 2. The module POSTs ``initialize`` with elicitation capabilities.
 * 3. Stores the ``MCP-Session-Id`` from the response header.
 * 4. Opens a ``fetch``-based SSE stream on ``GET /api/mcp``.
 * 5. Dispatches incoming ``elicitation/create`` events to registered handlers.
 * 6. Registered handlers POST the human's response back via ``sendMcpResponse``.
 * 7. On page unload, calls ``terminateMcpSession`` (DELETE /api/mcp).
 *
 * Why fetch instead of EventSource
 * ---------------------------------
 * The native ``EventSource`` API does not support custom request headers.
 * The MCP 2025-11-25 spec requires ``MCP-Session-Id`` on every request,
 * including the SSE GET.  We use ``fetch`` with a ``ReadableStream`` body
 * to parse SSE events while retaining full header control.
 */

// ── Types ──────────────────────────────────────────────────────────────────

/** A JSON-RPC 2.0 request sent by the server (e.g. elicitation/create). */
export interface McpServerRequest {
  jsonrpc: '2.0';
  id: string | number;
  method: string;
  params: unknown;
}

/** MCP elicitation/create params (form mode). */
export interface ElicitationCreateParams {
  mode: 'form' | 'url';
  message: string;
  requestedSchema: ElicitationSchema;
}

/** Flat JSON Schema for an elicitation form (spec: object with primitive props). */
export interface ElicitationSchema {
  type: 'object';
  properties: Record<string, ElicitationFieldSchema>;
  required?: string[];
}

/** Per-field schema inside an elicitation form. */
export interface ElicitationFieldSchema {
  type: string;
  title?: string;
  description?: string;
  default?: string | number | boolean;
  enum?: string[];
  format?: string;
  minimum?: number;
  maximum?: number;
}

/** Handler registered for a specific MCP server method. */
export type McpMethodHandler = (params: unknown, id: string | number) => void;

/** The result an elicitation handler sends back to the server. */
export interface ElicitationResponse {
  action: 'accept' | 'decline' | 'cancel';
  content?: Record<string, string | number | boolean>;
}

// ── State ──────────────────────────────────────────────────────────────────

let _sessionId: string | null = null;
let _sseController: AbortController | null = null;
const _handlers: Map<string, McpMethodHandler> = new Map();

// ── Public API ─────────────────────────────────────────────────────────────

/** Return the current MCP session ID, or null if not initialized. */
export function getMcpSessionId(): string | null {
  return _sessionId;
}

/**
 * Register a handler for a server-initiated MCP method.
 *
 * @example
 * registerMcpHandler('elicitation/create', (params, id) => {
 *   const p = params as ElicitationCreateParams;
 *   showElicitationModal(p, id);
 * });
 */
export function registerMcpHandler(method: string, handler: McpMethodHandler): void {
  _handlers.set(method, handler);
}

/**
 * POST a JSON-RPC response back to the server.
 *
 * Called by elicitation handlers after the human submits the form.
 */
export async function sendMcpResponse(
  id: string | number,
  result: ElicitationResponse,
): Promise<void> {
  if (!_sessionId) {
    console.warn('[mcp_session] sendMcpResponse called without active session');
    return;
  }
  const body: Record<string, unknown> = {
    jsonrpc: '2.0',
    id,
    result,
  };
  try {
    await fetch('/api/mcp', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'MCP-Session-Id': _sessionId,
        'MCP-Protocol-Version': '2025-11-25',
      },
      body: JSON.stringify(body),
    });
  } catch (err) {
    console.error('[mcp_session] failed to send response:', err);
  }
}

/**
 * Initialize the MCP session with the server.
 *
 * Posts an ``initialize`` request with elicitation capabilities, stores the
 * returned session ID, then opens the SSE stream.
 * Idempotent — safe to call multiple times; subsequent calls are no-ops.
 */
export async function initMcpSession(): Promise<void> {
  if (_sessionId) return;

  const initBody = {
    jsonrpc: '2.0',
    id: 1,
    method: 'initialize',
    params: {
      protocolVersion: '2025-11-25',
      capabilities: {
        elicitation: { form: {} },
      },
      clientInfo: {
        name: 'agentception-dashboard',
        version: '1.0',
      },
    },
  };

  let resp: Response;
  try {
    resp = await fetch('/api/mcp', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'MCP-Protocol-Version': '2025-11-25',
      },
      body: JSON.stringify(initBody),
    });
  } catch (err) {
    console.error('[mcp_session] initialize failed:', err);
    return;
  }

  const sid = resp.headers.get('MCP-Session-Id');
  if (!sid) {
    console.warn('[mcp_session] initialize response missing MCP-Session-Id header');
    return;
  }

  _sessionId = sid;
  console.info('[mcp_session] initialized, session:', sid.slice(0, 8));

  // Send the initialized notification (no response expected)
  void fetch('/api/mcp', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'MCP-Session-Id': sid,
      'MCP-Protocol-Version': '2025-11-25',
    },
    body: JSON.stringify({ jsonrpc: '2.0', method: 'initialized' }),
  }).catch(() => {/* swallow — notification only */});

  _openSseStream(sid);
}

/**
 * Terminate the MCP session (DELETE /api/mcp).
 *
 * Cancels the SSE stream and clears the session ID.
 * Call on page unload.
 */
export async function terminateMcpSession(): Promise<void> {
  if (!_sessionId) return;

  _sseController?.abort();
  _sseController = null;

  const sid = _sessionId;
  _sessionId = null;

  try {
    await fetch('/api/mcp', {
      method: 'DELETE',
      headers: {
        'MCP-Session-Id': sid,
        'MCP-Protocol-Version': '2025-11-25',
      },
      keepalive: true,
    });
  } catch {
    // Best-effort on unload
  }
  console.info('[mcp_session] terminated:', sid.slice(0, 8));
}

// ── SSE stream ─────────────────────────────────────────────────────────────

/**
 * Open a ``fetch``-based SSE stream.  Reconnects with exponential backoff
 * when the connection drops unexpectedly.
 */
function _openSseStream(sid: string): void {
  _sseController?.abort();
  _sseController = new AbortController();
  void _sseLoop(sid, _sseController.signal);
}

async function _sseLoop(sid: string, signal: AbortSignal): Promise<void> {
  let backoffMs = 1000;
  while (!signal.aborted) {
    try {
      await _sseConnect(sid, signal);
    } catch (err) {
      if (signal.aborted) break;
      console.warn(`[mcp_session] SSE disconnected, reconnecting in ${backoffMs}ms`, err);
      await _sleep(backoffMs);
      backoffMs = Math.min(backoffMs * 2, 30_000);
    }
  }
}

async function _sseConnect(sid: string, signal: AbortSignal): Promise<void> {
  const resp = await fetch('/api/mcp', {
    method: 'GET',
    headers: {
      Accept: 'text/event-stream',
      'MCP-Session-Id': sid,
      'MCP-Protocol-Version': '2025-11-25',
    },
    signal,
  });

  if (!resp.ok) {
    throw new Error(`SSE GET failed: ${resp.status}`);
  }

  const body = resp.body;
  if (!body) throw new Error('SSE response has no body');

  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const events = buffer.split('\n\n');
    buffer = events.pop() ?? '';

    for (const raw of events) {
      _handleSseEvent(raw.trim());
    }
  }
}

function _handleSseEvent(raw: string): void {
  if (!raw || raw.startsWith(':')) return; // keepalive comment

  let dataLine = '';
  for (const line of raw.split('\n')) {
    if (line.startsWith('data: ')) {
      dataLine = line.slice(6);
    }
  }
  if (!dataLine) return;

  let msg: unknown;
  try {
    msg = JSON.parse(dataLine);
  } catch {
    console.warn('[mcp_session] SSE: could not parse data:', dataLine);
    return;
  }

  if (!isRecord(msg) || msg['jsonrpc'] !== '2.0') return;

  const method = msg['method'];
  const id = msg['id'];
  if (typeof method !== 'string') return;

  const handler = _handlers.get(method);
  if (!handler) {
    console.warn('[mcp_session] no handler for method:', method);
    return;
  }

  if (typeof id !== 'string' && typeof id !== 'number') {
    console.warn('[mcp_session] server request missing id:', msg);
    return;
  }

  handler(msg['params'], id);
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function _sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
