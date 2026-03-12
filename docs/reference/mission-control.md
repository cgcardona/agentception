# Mission Control

Mission Control is the primary operator dashboard for the AgentCeption pipeline.
It is served at `/ship/<repo>/<initiative>` and auto-refreshes the phase board
every 5 seconds.

## Controls

### Force resync

The **Force resync** button (🔃) in the Mission Control header triggers a full
GitHub issue sync — fetching all open issues and up to 1 000 recently-closed
issues from the configured repository — without restarting the server or waiting
for the next poller tick.

Use it when you have just created, labelled, or closed issues on GitHub and want
the board to reflect those changes immediately rather than waiting for the next
automatic poll cycle.

The button POSTs to `POST /api/control/resync-issues` via HTMX and displays an
inline confirmation (open / closed / upserted counts) or an error message
directly below the button — no full page reload occurs.

See also: [`POST /api/control/resync-issues`](api.md#post-apicontrolresync-issues).
