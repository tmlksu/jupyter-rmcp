# Headless Jupyter Server config for jupyter-rmcp.
# This server is INTERNAL-ONLY (no host port, reachable only by the MCP
# container on the compose network). The only internet-facing surface is the
# MCP server. Auth to this server is the shared JUPYTER_TOKEN (set via env by
# the docker-stacks start script as --IdentityProvider.token).
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

# Same-cluster access from the MCP container. Safe because the network is
# private (no host/port exposure). Simplifies REST+WS API calls.
c.ServerApp.allow_origin = "*"
c.ServerApp.disable_check_xsrf = True

# Kernel lifecycle: the MCP server owns reaping (single source of truth), so
# disable the server's own culler to avoid double-reaping surprises.
c.MappingKernelManager.cull_idle_timeout = 0
c.MappingKernelManager.cull_connected = False
