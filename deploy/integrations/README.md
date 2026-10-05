# Integrations catalog

Each integration an agent can be given (a mailbox, a CRM, a file store...) is
described by one JSON file here. agentdesk reads this directory from its
pinned neurobot checkout, so **adding an integration is a new file (plus an
optional tool and check script) and a new neurobot tag -- no agentdesk code
change**.

```
<id>.json            the spec (below)
<id>.md              the section written into an agent's instructions when it is connected
<id>.check.py        optional: run on the agent's server to verify the connection
```

## Spec

```json
{
  "id": "bitrix24",
  "title": "Bitrix24",
  "category": "CRM",
  "icon": "crm",
  "color": "#2FC6F6",
  "icon_asset": "assets/brand/bitrix24.png",
  "description": "one or two sentences shown in the catalog",
  "help": "optional longer how-to (plain text, blank line = new paragraph)",
  "fields": [
    {"key": "BITRIX_WEBHOOK_URL", "label": "Вебхук", "secret": true, "required": true,
     "placeholder": "https://…", "helper": "short hint under the field", "default": ""}
  ],
  "configured_when": ["BITRIX_WEBHOOK_URL"],
  "instructions_file": "bitrix24.md",
  "check_file": "bitrix24.check.py",
  "tool": {"file": "deploy/agent-tools/agent-x.py", "dest": "/usr/local/bin/agent-x"}
}
```

* `icon` / `color` (optional): the integration's badge in agentdesk. `icon` is
  a name from the panel's built-in set (`crm`, `login`, `mail`, `cloud`,
  `folder`, `chat`, `calendar`, `database`, `code`, `phone`, `payments`,
  `cart`, `document`, `table`, `api`, `key`) or a single emoji; anything
  else falls back to a generic plug. `color` is `#RRGGBB`.
* `icon_asset` (optional): the service's own logo instead of `icon`/`color` --
  a PNG bundled into the panel at `app/assets/brand/<id>.png` (added there by
  hand, from the service's own site; the panel ships no logo-fetching code).
  Must be exactly `assets/brand/<lowercase-id>.png`; when set, it replaces
  `icon`/`color` in every badge, everywhere this integration is shown.
* `fields[].key` are environment variable names (A-Z, 0-9, _). Values are stored
  only on the agent's own server, in a root-only env file handed to its
  service; agentdesk never keeps or shows them.
* `configured_when` lists the keys that must all be set for the integration to
  count as connected (default: every `required` field).
* `fields[].show_when` (optional): `{"OTHER_KEY": "value"}` -- the field is shown
  (and required, and kept) only while the dropdown field `OTHER_KEY` has that
  value. A field that is hidden loses its stored value, so changing the choice
  removes the other choice's secrets from agentdesk and from the agent's server.
  Used by Bitrix24: one card, a "way of connecting" dropdown, the webhook field
  or the login fields below it.
* `configured_when_any` (optional): a list of key lists; the integration counts
  as connected when ALL keys of ANY of the lists are set (use instead of
  `configured_when` when there are alternative ways in).
* `aliases` (optional): `[{"id": "old-id", "values": {"KEY": "value"}}]` -- an
  integration this one replaces. agentdesk moves connections made under the old
  id onto this one and sets the given values; the variables on agents' servers
  keep their names, so nothing needs to be re-entered.
* `presets` (optional): buttons that fill several fields at once (a mail
  provider's servers); `prefill` on a field is a suggestion for a new connection.
* `tool` (optional): a small command-line tool installed for the agent to use.
  `dest` must be under `/usr/local/bin/`.

## Generic integrations

Three entries in the `Универсальные` category let a person connect something
that has no entry of its own:

* `custom-api` -- any REST API. Base address, sign-in method, secret, optional
  OpenAPI address; the agent uses `agent-http` (`deploy/agent-tools/agent-http.py`),
  which adds the secret itself, stays on the connected host, is read-only
  unless the connection allows writing, and cuts big answers.
* `custom-webhook` -- inbound events. `"listener": "webhook"` makes agentdesk
  accept `POST /api/hooks/<token>` for the connection and store the body as a
  message in a channel the agent reads with `agent-chat`. Its fields are all
  `agentdesk_only`.
* `custom-mcp` -- an MCP server. The `"mcp"` object maps spec fields
  (`url`, `transport`, `token`, `header`, `scheme`) to what agentdesk writes
  into the agent's project `.mcp.json` as `ad-<name>`: URL and header values
  are `${VAR}` references to the connection's variables, so no secret lands in
  the file. The agent sees the tools after a restart.

## Check script contract

Runs as root on the agent's server with the agent's integration variables in
`os.environ`. It must print ONE JSON line:

```json
{"status": "ok|warn|fail|unconfigured", "summary": "short line",
 "items": [{"level": "ok|warn|fail", "text": "what was checked"}]}
```

A check must never modify anything on the remote system.
