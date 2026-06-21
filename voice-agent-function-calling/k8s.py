"""Read-only Kubernetes access for the OnCall agent.

Shells out to `kubectl` using a dedicated read-only kubeconfig (the `oncall-readonly`
ServiceAccount: built-in `view` role + nodes/namespaces/metrics, no Secrets, no
writes). RBAC is the safety boundary — even if the model asks to delete something,
the kubeconfig can't. Every function returns plain dicts/lists for the agent to
read aloud.

Kubeconfig resolution: env ONCALL_KUBECONFIG, else /tmp/oncall-kubeconfig if it
exists, else the caller's default kubeconfig.
"""
import os
import json
import subprocess

_DEFAULT_KC = "/tmp/oncall-kubeconfig"
_TIMEOUT = 15


def _kubeconfig():
    kc = os.environ.get("ONCALL_KUBECONFIG")
    if kc:
        return kc
    if os.path.exists(_DEFAULT_KC):
        return _DEFAULT_KC
    return None  # fall back to kubectl's default resolution


def _kubectl(args, parse_json=True):
    """Run a read-only kubectl command. Returns parsed JSON (or text), or {'error': ...}."""
    cmd = ["kubectl"]
    kc = _kubeconfig()
    if kc:
        cmd += ["--kubeconfig", kc]
    cmd += ["--request-timeout=10s"] + args
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"error": "kubectl timed out"}
    except FileNotFoundError:
        return {"error": "kubectl not found on PATH"}
    if out.returncode != 0:
        return {"error": (out.stderr or out.stdout or "kubectl failed").strip()[:400]}
    if not parse_json:
        return out.stdout
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return {"error": "could not parse kubectl output"}


def _pod_summary(item):
    meta = item.get("metadata", {})
    status = item.get("status", {})
    cs = status.get("containerStatuses", []) or []
    ready = sum(1 for c in cs if c.get("ready"))
    total = len(cs)
    restarts = sum(c.get("restartCount", 0) for c in cs)
    # Surface the most useful "why": a waiting/terminated reason beats phase.
    reason = status.get("phase", "Unknown")
    for c in cs:
        st = c.get("state", {})
        if "waiting" in st and st["waiting"].get("reason"):
            reason = st["waiting"]["reason"]
            break
        if "terminated" in st and st["terminated"].get("reason"):
            reason = st["terminated"]["reason"]
    return {
        "namespace": meta.get("namespace"),
        "name": meta.get("name"),
        "status": reason,
        "ready": f"{ready}/{total}",
        "restarts": restarts,
    }


def get_pods(namespace=None, all_pods=False):
    """List pods. With no namespace, scans the whole cluster and (unless all_pods)
    returns only pods that are NOT healthy/Running — i.e. what's broken right now."""
    args = ["get", "pods", "-o", "json"]
    args += ["-n", namespace] if namespace else ["-A"]
    data = _kubectl(args)
    if "error" in data:
        return data
    pods = [_pod_summary(it) for it in data.get("items", [])]

    def is_unhealthy(p):
        if p["status"] in {"Succeeded", "Completed"}:
            return False  # finished jobs aren't broken even at 0/1 ready
        if p["status"] != "Running":
            return True
        r = p["ready"].split("/")
        return r[0] != r[1]  # Running but not all containers ready

    if not all_pods and not namespace:
        unhealthy = [p for p in pods if is_unhealthy(p)]
        return {"unhealthy_pods": unhealthy, "total_pods": len(pods),
                "unhealthy_count": len(unhealthy)}
    return {"pods": pods, "count": len(pods)}


def describe_pod(name, namespace="default"):
    """Root-cause view for one pod: container states/reasons + recent events."""
    pod = _kubectl(["get", "pod", name, "-n", namespace, "-o", "json"])
    if "error" in pod:
        return pod
    status = pod.get("status", {})
    containers = []
    for c in status.get("containerStatuses", []) or []:
        st = c.get("state", {})
        phase = next(iter(st), "unknown")
        detail = st.get(phase, {})
        containers.append({
            "container": c.get("name"),
            "state": phase,
            "reason": detail.get("reason"),
            "message": (detail.get("message") or "")[:200],
            "exit_code": detail.get("exitCode"),
            "restarts": c.get("restartCount", 0),
        })
    ev = _kubectl(["get", "events", "-n", namespace,
                   "--field-selector", f"involvedObject.name={name}",
                   "-o", "json"])
    events = []
    if "error" not in ev:
        items = sorted(ev.get("items", []),
                       key=lambda e: e.get("lastTimestamp") or "", reverse=True)
        for e in items[:6]:
            events.append({"type": e.get("type"), "reason": e.get("reason"),
                           "message": (e.get("message") or "")[:200]})
    return {"pod": name, "namespace": namespace, "phase": status.get("phase"),
            "containers": containers, "recent_events": events}


def get_pod_logs(name, namespace="default", lines=50, previous=False):
    """Tail recent logs for a pod. previous=True reads the last crashed container."""
    args = ["logs", name, "-n", namespace, f"--tail={int(lines)}"]
    if previous:
        args.append("--previous")
    text = _kubectl(args, parse_json=False)
    if isinstance(text, dict) and "error" in text:
        # Crashed pods often have logs only under --previous; retry once.
        if not previous:
            return get_pod_logs(name, namespace, lines, previous=True)
        return text
    log_lines = [l for l in text.splitlines() if l.strip()]
    return {"pod": name, "namespace": namespace, "previous": previous,
            "lines": log_lines[-int(lines):]}


def get_events(namespace=None, warnings_only=True):
    """Recent cluster events (Warnings by default) — a fast 'what's wrong' scan."""
    args = ["get", "events", "-o", "json"]
    args += ["-n", namespace] if namespace else ["-A"]
    if warnings_only:
        args += ["--field-selector", "type=Warning"]
    data = _kubectl(args)
    if "error" in data:
        return data
    items = sorted(data.get("items", []),
                   key=lambda e: e.get("lastTimestamp") or "", reverse=True)
    events = []
    for e in items[:12]:
        obj = e.get("involvedObject", {})
        events.append({
            "namespace": e.get("metadata", {}).get("namespace"),
            "object": f"{obj.get('kind')}/{obj.get('name')}",
            "reason": e.get("reason"),
            "message": (e.get("message") or "")[:200],
        })
    return {"warnings": events, "count": len(events)}


if __name__ == "__main__":
    import sys
    fn = sys.argv[1] if len(sys.argv) > 1 else "get_pods"
    print(json.dumps(globals()[fn](*sys.argv[2:]), indent=2))
