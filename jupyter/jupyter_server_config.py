# Headless Jupyter Server config for jupyter-rmcp.
# This server is INTERNAL-ONLY (no host port, reachable only by the MCP
# container on the compose network). The only internet-facing surface is the
# MCP server. Auth to this server is the shared JUPYTER_TOKEN (set via env by
# the docker-stacks start script as --IdentityProvider.token).
c = get_config()  # noqa: F821

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
