# Integration Guide

This guide covers how to integrate external tools, scripts, and workflows with AgentCeption's browser UI and localStorage state.

---

## Batch Context Bar

The **batch context bar** is a slim persistent strip rendered immediately below the top navigation on every page. It shows the currently active `batch_id` and provides quick navigation links to the Plan, Build, and Ship pages for that batch.

### How it works

The bar is driven entirely by `localStorage`. No server session is involved. When a batch is started (e.g. from the Plan page or via the MCP `build_kickoff` tool), the following keys are written:

| Key | Type | Description |
|-----|------|-------------|
| `ac_active_batch` | `string` | The active `batch_id` (e.g. `eng-20260304T230644Z-5a86`) |
| `ac_active_initiative` | `string` | The active initiative name used for the Build page link |

The bar renders only when `ac_active_batch` is non-empty. It hides automatically when the key is absent or blank.

### Dismissing the bar

Clicking the **✕** button calls `dismiss()`, which:

1. Removes `ac_active_batch` from `localStorage`.
2. Removes `ac_active_initiative` from `localStorage`.
3. Hides the bar immediately via Alpine's `x-show` binding.

The same effect can be triggered programmatically from any page:

```js
localStorage.removeItem('ac_active_batch');
localStorage.removeItem('ac_active_initiative');
```

### Setting the active batch from code

Any page or script can activate the bar by writing to `localStorage`:

```js
localStorage.setItem('ac_active_batch', 'eng-20260304T230644Z-5a86');
localStorage.setItem('ac_active_initiative', 'my-initiative-name');
```

The Alpine component listens for `storage` events, so the bar updates instantly in any tab that has the page open.

### Navigation links

| Link | Destination |
|------|-------------|
| Plan | `/plan` |
| Build | `/build?initiative=<ac_active_initiative>` |
| Ship | `/ship?batch=<ac_active_batch>` |

Build and Ship pages should call `localStorage.setItem(...)` on `init()` when the corresponding query param is present, so that arriving via a direct link also populates the bar.

### Alpine component

The component lives in `agentception/static/js/base.js` and is exported as `batchBar`. It is registered globally in `app.js` and referenced in `base.html` via:

```html
<div class="batch-bar"
     x-data="batchBar()"
     x-show="batchId"
     x-init="init()"
     x-cloak>
  ...
</div>
```

The `x-cloak` attribute prevents a flash of the bar before Alpine initialises. The `[x-cloak] { display: none !important }` rule is defined in `_foundation.scss`.
