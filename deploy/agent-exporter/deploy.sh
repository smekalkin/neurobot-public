#!/usr/bin/env bash
# Deploy (or update) agent_exporter.py on a managed server. Part of neurobot:
# the exporter watches the server's agents and pushes their live status to
# agentdesk (POST /api/fleet/heartbeat). Nothing here touches Prometheus or
# any monitoring stack. Re-run to update — every step here is idempotent.
#
# Usage:
#   deploy.sh <host> <ssh_user@ssh_host> <ssh_port> <ssh_key>
#
# <host>   matches hosts/<host>.fleet.json and hosts/<host>.env in this
#          directory (see server-b/server-a/demo for examples). <host> must equal the
#          server's name in agentdesk.
#
# AGENTDESK_URL / AGENT_EXPORTER_TOKEN are secrets and never live in git: set
# them once in the server's /etc/agent-exporter.env (the token comes from
# agentdesk: POST /api/servers/{id}/heartbeat-token). Re-deploys keep them.
set -euo pipefail

HOST="${1:?usage: deploy.sh <host> <ssh_user@ssh_host> <ssh_port> <ssh_key>}"
SSH_TARGET="${2:?ssh_user@ssh_host required}"
SSH_PORT="${3:?ssh_port required}"
SSH_KEY="${4:?ssh_key required}"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLEET_FILE="$DIR/hosts/$HOST.fleet.json"
ENV_FILE="$DIR/hosts/$HOST.env"

[ -f "$FLEET_FILE" ] || { echo "missing $FLEET_FILE" >&2; exit 1; }
[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE" >&2; exit 1; }

ssh_cmd() { ssh -i "$SSH_KEY" -p "$SSH_PORT" -o ConnectTimeout=15 -o BatchMode=yes "$SSH_TARGET" "$@"; }
scp_cmd() { scp -i "$SSH_KEY" -P "$SSH_PORT" -o ConnectTimeout=15 -o BatchMode=yes "$@"; }

echo "== staging /opt/agent-exporter on $SSH_TARGET ==" >&2
ssh_cmd "sudo mkdir -p /opt/agent-exporter/hosts"
scp_cmd "$DIR/agent_exporter.py" "$SSH_TARGET:/tmp/agent_exporter.py"
scp_cmd "$FLEET_FILE" "$SSH_TARGET:/tmp/$HOST.fleet.json"
scp_cmd "$DIR/agent-exporter.service" "$SSH_TARGET:/tmp/agent-exporter.service"
scp_cmd "$ENV_FILE" "$SSH_TARGET:/tmp/agent-exporter.env"

ssh_cmd "
  set -e
  sudo mv /tmp/agent_exporter.py /opt/agent-exporter/agent_exporter.py
  sudo mv /tmp/$HOST.fleet.json /opt/agent-exporter/hosts/$HOST.fleet.json
  sudo mv /tmp/agent-exporter.service /etc/systemd/system/agent-exporter.service
  # Secrets and local tuning are deliberately absent from hosts/*.env in
  # git. Preserve existing keys that the fresh file does not define.
  # Use sudo cat because the file owner and sudoers policy can vary by host.
  # The temporary file belongs to this connection, so appending needs no sudo.
  if sudo test -f /etc/agent-exporter.env; then
    sudo cat /etc/agent-exporter.env | while IFS='=' read -r key val; do
      case \"\$key\" in ''|'#'*) continue ;; esac
      grep -q \"^\${key}=\" /tmp/agent-exporter.env || echo \"\${key}=\${val}\" >> /tmp/agent-exporter.env
    done
  fi
  sudo mv /tmp/agent-exporter.env /etc/agent-exporter.env
  sudo chmod +x /opt/agent-exporter/agent_exporter.py
  sudo chown -R root:root /opt/agent-exporter
  echo '-- smoke test (--once) --'
  sudo AGENT_EXPORTER_CONFIG=/opt/agent-exporter/hosts/$HOST.fleet.json python3 /opt/agent-exporter/agent_exporter.py --once | head -5
  echo '-- systemd --'
  sudo systemctl daemon-reload
  # enable --now does not restart an already-active service, so force a real
  # restart after deployment.
  sudo systemctl enable agent-exporter.service
  sudo systemctl restart agent-exporter.service
  sudo systemctl is-active agent-exporter.service
"

echo "== done: agent-exporter deployed to $SSH_TARGET ==" >&2
