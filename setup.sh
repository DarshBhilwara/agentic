#!/bin/bash
set -euo pipefail

NODE_ROLE="${1:-}"
case "$NODE_ROLE" in
  inference)
    BASE_DIR="/home/iiitd/Documents/agentic"
    ;;
  agent)
    BASE_DIR="$(pwd -P)"
    ;;
  *)
    echo "Usage: $0 {inference|agent}" >&2
    exit 2
    ;;
esac

echo "=> Initializing Agentic Platform for the $NODE_ROLE node..."

echo "=> Creating directory anchors at $BASE_DIR"
mkdir -p "$BASE_DIR/workspace/incoming" \
         "$BASE_DIR/workspace/processed" \
         "$BASE_DIR/workspace/users" \
         "$BASE_DIR/manifests" \
         "$BASE_DIR/model-cache"

if [[ "$NODE_ROLE" == "agent" && -f "$BASE_DIR/manifests/agent-node.yaml" ]]; then
  sed -i "s|__AGENT_BASE_DIR__|$BASE_DIR|g" "$BASE_DIR/manifests/agent-node.yaml"
  sed -i "s|__AGENT_WORKSPACE_ROOT__|$(dirname "$BASE_DIR")|g" "$BASE_DIR/manifests/agent-node.yaml"
fi

chmod -R 777 "$BASE_DIR/workspace"
chmod -R 777 "$BASE_DIR/model-cache"

# The inference host bootstraps the K3s server. The agent host joins that
# server using credentials supplied in K3S_URL and K3S_TOKEN.
if [[ "$NODE_ROLE" == "inference" ]]; then
  echo "=> Installing K3s server..."
  curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--disable traefik" sh -
else
  : "${K3S_URL:?Set K3S_URL to the inference node API endpoint, e.g. https://inference-host:6443}"
  : "${K3S_TOKEN:?Set K3S_TOKEN to the contents of /var/lib/rancher/k3s/server/node-token on the inference node}"
  echo "=> Joining K3s at $K3S_URL..."
  curl -sfL https://get.k3s.io | K3S_URL="$K3S_URL" K3S_TOKEN="$K3S_TOKEN" sh -
fi

if [[ "$NODE_ROLE" == "inference" ]]; then

  echo "=> Configuring NVIDIA Container Runtime..."
  mkdir -p /var/lib/rancher/k3s/agent/etc/containerd/
  cat <<EOF > /var/lib/rancher/k3s/agent/etc/containerd/config.toml.tmpl
[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runc]
  runtime_type = "io.containerd.runc.v2"
[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runc.options]
  SystemdCgroup = true

[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.nvidia]
  privileged_without_host_devices = false
  runtime_engine = ""
  runtime_root = ""
  runtime_type = "io.containerd.runc.v2"
[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.nvidia.options]
  BinaryName = "/usr/bin/nvidia-container-runtime"
  SystemdCgroup = true
EOF

  echo "=> Restarting K3s to apply GPU configuration..."
  systemctl restart k3s

  echo "=> Setup complete. Kubeconfig is at /etc/rancher/k3s/k3s.yaml"
else
  echo "=> Agent-node directory and K3s setup complete at $BASE_DIR"
fi
