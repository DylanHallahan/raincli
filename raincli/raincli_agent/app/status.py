"""What the tray shows, from the runtime's local status record. Stdlib only."""
import time

ICON_STATES = ("ready", "offline", "updating", "error")


def icon_state(status):
    """``ready``, ``offline``, ``updating`` or ``error`` for ``runtime.service.status``'s record."""
    if not isinstance(status, dict) or status.get("status") in (None, "not_observed", "stopped", "starting"):
        return "offline"
    client = status.get("client") or {}
    if client.get("update_state") == "updating":
        return "updating"
    if status.get("error") or client.get("update_state") in ("failed", "rolled_back"):
        return "error"
    if status.get("stale"):
        return "offline"
    entries = status.get("connectors") or []
    if not entries or not any(e.get("reported") for e in entries):
        return "offline"
    if any(e.get("error") for e in entries):
        return "error"
    return "ready"


def ready(status):
    """The runtime is running and has published presence (migration step 4)."""
    return icon_state(status) in ("ready", "updating") or (
        isinstance(status, dict) and status.get("status") == "running"
        and any(e.get("reported") for e in status.get("connectors") or []))


def describe(status, *, handle=None, machine=None, version=None, update_mode=None, agents=None, paused=False):
    """Lines for the status window: connection, machine, version and update state, agents."""
    state = "paused" if paused else icon_state(status)
    client = (status or {}).get("client") or {}
    lines = [f"Connection: {state}"]
    if machine or handle:
        lines.append(f"Machine: {handle or machine}")
    lines.append(f"Version: {client.get('version') or version or 'unknown'}")
    update = client.get("update_state") or "current"
    error = client.get("error")
    lines.append(f"Updates: {client.get('update_mode') or update_mode or 'automatic'}, {update}"
                 + (f" ({error})" if error else ""))
    if isinstance(status, dict) and isinstance(status.get("updated_at"), (int, float)):
        lines.append("Last report: " + time.strftime("%H:%M:%S", time.localtime(status["updated_at"])))
    if agents is not None:
        lines.append(f"Agents on this machine: {len(agents)}")
        for agent in agents[:20]:
            role = " (inbox)" if agent.get("role") == "inbox" else ""
            lines.append(f"  {agent.get('name')}  {agent.get('type')}  {agent.get('status')}{role}")
    return lines
