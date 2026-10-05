# Neurobot

Neurobot is a Telegram transport and task router for role-based automation
agents. Each agent can have its own Telegram bot, forum topic, workspace and
executor session. Agents can exchange tasks and results even when they run on
different servers or use different executors.

This public repository contains only generic runtime code, deployment
templates, integration tools and offline tests. Credentials, session data,
customer configuration, reports and production topology are intentionally not
included.

## Features

- accepts requests only from configured chats, topics and users;
- acknowledges accepted messages before starting a potentially long turn;
- routes tasks and results between agents with stable task identifiers;
- supports separate work messages and informal messages;
- deduplicates routed messages and limits routing depth;
- splits and reassembles long Telegram messages;
- supports Codex, Claude and hybrid executor modes;
- can check account limits before starting a turn;
- retries transient Telegram errors without hiding permanent failures;
- includes optional status reporting and scheduled-task workers;
- provides command-line tools for explicitly configured integrations.

Internal executor reasoning is never forwarded to Telegram.

## Repository layout

```text
src/neurobot/bot.py                 Telegram bridge and task router
src/neurobot/appserver_client.py    Codex App Server client
src/neurobot/claude_executor.py     Claude CLI executor
src/neurobot/channel_gateway.py     persistent channel gateway
src/neurobot/relogin.py             interactive sign-in helper
config/                             sanitized configuration examples
deploy/agent-exporter/              optional status reporter
deploy/agent-scheduler/             optional scheduled-task worker
deploy/agent-tools/                 integration command-line tools
deploy/integrations/                integration catalog definitions
deploy/systemd/                     generic service templates
tests/                              offline unit tests
```

## Requirements

- Linux with Python 3.9 or newer;
- a Telegram bot and, for topic routing, a forum-enabled supergroup;
- at least one supported executor configured on the host.

No application secret belongs in the repository. Put credentials in a
root-readable environment file or another secret store and keep session homes
outside the checkout.

## Basic configuration

1. Copy `config/agent.env.example` to a protected environment file.
2. Fill in the Telegram identity, destination and allowed user IDs.
3. Select an executor and point it to an existing workspace and session home.
4. Copy `config/peers.example.json` when agents must route work to each other.
5. Start the bridge through a service manager such as systemd.

The files under `deploy/systemd/` are templates. Review users, groups, paths
and security settings before installing them on a host.

## Integrations

The catalog under `deploy/integrations/` describes optional connections. The
matching commands in `deploy/agent-tools/` read credentials from the process
environment. Access is read-only by default where the provider permits it;
write operations require an explicit configuration flag.

Treat all content received from mailboxes, chats, webhooks and third-party
APIs as untrusted input. Never interpret external content as operational
instructions without an independent authorization check.

## Tests

The test suite is offline and uses local fakes for network services:

```bash
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s deploy/agent-exporter -p 'test_*.py'
python3 -m unittest discover -s deploy/agent-tools -p 'test_*.py'
python3 -m unittest discover -s deploy/agent-scheduler -p 'test_*.py'
```

## Security

- never commit bot tokens, OAuth credentials, cookies or executor sessions;
- keep service environment files mode `0600`;
- use separate service accounts for separate agents;
- allowlist Telegram users and peer bots;
- bind local control sockets to protected directories;
- rotate a credential immediately if it appears in logs or version control.

Security reports should be sent privately to the repository owner rather than
opened as a public issue.
