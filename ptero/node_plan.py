"""Collect node connection details without network or filesystem operations."""
from .core import Cancelled, valid_url


def connection(installer, known_url=None):
    ui = installer.ui
    ui.say("In the Panel: Admin → Locations → create a location; Nodes → create a node → Configuration.")
    ui.say("Set the domain, API/SFTP ports, memory, disk and allocations using the node's IP.")
    ui.say("Use the Auto-Deploy token. Your input will be hidden.")
    url = ui.ask("Panel URL", validator=valid_url).rstrip("/")
    if url.startswith("http:") and url != known_url:
        if not ui.yes("The Panel uses HTTP. Continue linking this node without HTTPS?"):
            raise Cancelled("Node linking cancelled.")
    node = ui.ask("Numeric node ID", validator=lambda s: s.isdigit() and int(s) > 0)
    token = ui.ask("Auto-Deploy token", secret=True,
                   validator=lambda s: bool(s) and not any(c.isspace() for c in s))
    installer.run.secrets.add(token)
    ui.confirm("CONFIGURE WINGS")
    return url, node, token
