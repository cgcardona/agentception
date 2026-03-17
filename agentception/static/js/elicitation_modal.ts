/**
 * elicitation_modal.ts — Alpine.js component for MCP elicitation/create.
 *
 * Renders a form modal when an MCP server pushes an ``elicitation/create``
 * request via SSE.  The human fills in the form and clicks Submit/Decline.
 * The component calls ``sendMcpResponse`` to POST the JSON-RPC response back.
 *
 * Usage (registered globally in app.ts, placed once in base.html):
 *   <div x-data="elicitationModal()" …>
 *
 * The modal is triggered by the ``mcp:elicitation`` custom window event, which
 * is dispatched by the MCP session handler registered in initMcpSession().
 */

import {
  type ElicitationCreateParams,
  type ElicitationFieldSchema,
  type ElicitationResponse,
  sendMcpResponse,
} from './mcp_session.ts';

// ── Field model ────────────────────────────────────────────────────────────

/** Rendered form field with live value binding. */
interface FieldModel {
  name: string;
  schema: ElicitationFieldSchema;
  /** Current value as entered by the human. */
  value: string | number | boolean;
  /** True when the field is required and the current value is blank. */
  invalid: boolean;
}

// ── Alpine component ────────────────────────────────────────────────────────

export interface ElicitationModalComponent {
  open: boolean;
  message: string;
  fields: FieldModel[];
  submitting: boolean;
  _rpcId: string | number | null;
  show(params: ElicitationCreateParams, rpcId: string | number): void;
  dismiss(action: 'decline' | 'cancel'): void;
  submit(): Promise<void>;
  _validate(): boolean;
}

/**
 * Alpine component factory for the global elicitation modal.
 *
 * The modal listens for the ``mcp:elicitation`` window event and renders the
 * form fields from the ``requestedSchema`` in the event detail.
 */
export function elicitationModal(): ElicitationModalComponent {
  return {
    open: false,
    message: '',
    fields: [],
    submitting: false,
    _rpcId: null as string | number | null,

    show(params: ElicitationCreateParams, rpcId: string | number): void {
      this._rpcId = rpcId;
      this.message = params.message;
      this.submitting = false;

      const schema = params.requestedSchema ?? { type: 'object', properties: {} };
      const required = new Set<string>(schema.required ?? []);

      this.fields = Object.entries(schema.properties ?? {}).map(
        ([name, fieldSchema]): FieldModel => ({
          name,
          schema: fieldSchema,
          value: fieldSchema.default ?? (fieldSchema.type === 'boolean' ? false : ''),
          invalid: false,
        }),
      );

      // Sync the required flag into each field for validation display
      this.fields.forEach((f) => {
        if (required.has(f.name)) {
          (f as FieldModel & { _required?: boolean })._required = true;
        }
      });

      this.open = true;
    },

    dismiss(action: 'decline' | 'cancel'): void {
      if (!this._rpcId) return;
      const rpcId = this._rpcId;
      this.open = false;
      this._rpcId = null;
      this.fields = [];

      const response: ElicitationResponse = { action };
      void sendMcpResponse(rpcId, response);
    },

    _validate(): boolean {
      let valid = true;
      for (const field of this.fields) {
        const isRequired = !!(field as FieldModel & { _required?: boolean })._required;
        const isEmpty =
          field.value === '' || field.value === null || field.value === undefined;
        field.invalid = isRequired && isEmpty;
        if (field.invalid) valid = false;
      }
      return valid;
    },

    async submit(): Promise<void> {
      if (!this._validate()) return;
      if (!this._rpcId) return;

      this.submitting = true;
      const rpcId = this._rpcId;

      const content: Record<string, string | number | boolean> = {};
      for (const field of this.fields) {
        const v = field.value;
        if (v !== '' && v !== null && v !== undefined) {
          if (field.schema.type === 'number' || field.schema.type === 'integer') {
            content[field.name] = Number(v);
          } else if (field.schema.type === 'boolean') {
            content[field.name] = Boolean(v);
          } else {
            content[field.name] = String(v);
          }
        }
      }

      const response: ElicitationResponse = { action: 'accept', content };

      this.open = false;
      this._rpcId = null;
      this.fields = [];

      await sendMcpResponse(rpcId, response);
      this.submitting = false;
    },
  };
}

// ── Window event bridge ────────────────────────────────────────────────────

/**
 * Dispatch a ``mcp:elicitation`` window event so the Alpine component can
 * react without needing a direct reference to the component instance.
 *
 * Called from the MCP session's method handler registration in app.ts.
 */
export function dispatchElicitationEvent(
  params: ElicitationCreateParams,
  rpcId: string | number,
): void {
  window.dispatchEvent(
    new CustomEvent('mcp:elicitation', { detail: { params, rpcId } }),
  );
}
