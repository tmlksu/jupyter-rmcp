# Headless Jupyter Server (internal-only execution backend).
# base-notebook already ships jupyterlab + ipykernel; we add a default sci stack.
FROM quay.io/jupyter/base-notebook:latest

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Server config baked in; token is injected at runtime via JUPYTER_TOKEN env.
COPY jupyter_server_config.py /etc/jupyter/jupyter_server_config.py
