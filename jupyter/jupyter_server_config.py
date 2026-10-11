# Headless Jupyter Server config for jupyter-rmcp.
# This server is reachable two ways: by the MCP container on the compose network
# (REST+WS, JUPYTER_TOKEN header auth) and by a human via JupyterLab — a loopback
# host port by default, or a tunnel + authenticating proxy when published per
# ADR 0012. Auth is the shared JUPYTER_TOKEN (set via env by the docker-stacks
# start script as --IdentityProvider.token).
import os

c = get_config()  # noqa: F821

# The docker-stacks base image passes --IdentityProvider.token on the command
# line (CLI wins over this file, so setting it here changes nothing there). The
# slim image has no such start script, so read the env ourselves — one config
# file serves both.
if os.environ.get("JUPYTER_TOKEN"):
    c.IdentityProvider.token = os.environ["JUPYTER_TOKEN"]

c.ServerApp.ip = "0.0.0.0"                 # listen on the container network iface
c.ServerApp.port = 8888
c.ServerApp.open_browser = False
c.ServerApp.root_dir = "/home/jovyan/work"
c.ServerApp.allow_remote_access = True

# Keep the XSRF check and the default Origin policy ON (ADR 0020): Lab is exposed
# behind a proxy (ADR 0012), and a credentialed cross-site request must not be
# able to drive this server. The MCP is unaffected — it authenticates with the
# token header (exempt from XSRF) and its server-side REST/WS sends no Origin.

# Kernel lifecycle: the MCP server owns reaping (single source of truth), so
# disable the server's own culler to avoid double-reaping surprises.
c.MappingKernelManager.cull_idle_timeout = 0
c.MappingKernelManager.cull_connected = False
