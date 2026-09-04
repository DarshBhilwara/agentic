#!/bin/bash
set -e

echo "=> Initializing Enterprise Agentic Platform..."
BASE_DIR="/home/iiitd/Documents/agentic"

# 1. Anchor & create all required directories
echo "=> Creating directory anchors at $BASE_DIR"
mkdir -p "$BASE_DIR/workspace/incoming" \
         "$BASE_DIR/workspace/processed" \
         "$BASE_DIR/workspace/users" \
         "$BASE_DIR/manifests" \
         "$BASE_DIR/model-cache"

# 2. Force permissions to prevent container UID/GID mapping issues
chmod -R 777 "$BASE_DIR/workspace"
chmod -R 777 "$BASE_DIR/model-cache"

# 3. Install K3s (disabling Traefik so we control our own Gateway/NodePorts)
echo "=> Installing K3s..."
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--disable traefik" sh -

# 4. Configure K3s containerd for NVIDIA GPUs
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
