Apply the oncall-rbac.yaml
Part 1 — Build a read-only kubeconfig (5 min)
Run these to mint a token and assemble a standalone kubeconfig the agent will use:


# Get your cluster's server URL and CA from your current (admin) context
CLUSTER_NAME=$(kubectl config view --minify -o jsonpath='{.clusters[0].name}')
SERVER=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')
kubectl config view --minify --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d > /tmp/oncall-ca.crt

# Mint a short-lived read-only token
TOKEN=$(kubectl create token oncall-readonly -n default --duration=8h)

# Assemble a dedicated kubeconfig
kubectl --kubeconfig=/tmp/oncall-kubeconfig config set-cluster "$CLUSTER_NAME" \
  --server="$SERVER" --certificate-authority=/tmp/oncall-ca.crt --embed-certs=true
kubectl --kubeconfig=/tmp/oncall-kubeconfig config set-credentials oncall-readonly --token="$TOKEN"
kubectl --kubeconfig=/tmp/oncall-kubeconfig config set-context oncall \
  --cluster="$CLUSTER_NAME" --user=oncall-readonly
kubectl --kubeconfig=/tmp/oncall-kubeconfig config use-context oncall





Verify the lock works before wiring anything — this is the gate:
# Should SUCCEED (read):
kubectl --kubeconfig=/tmp/oncall-kubeconfig get pods -A
# Should be DENIED (write) — proving RBAC holds:
kubectl --kubeconfig=/tmp/oncall-kubeconfig delete ns default --dry-run=server

