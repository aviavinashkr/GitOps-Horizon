"""
engine/executor.py
──────────────────
Day 3: Runtime Execution Engine

Wraps helm and kubectl subprocess calls, captures exit codes and stderr,
and extracts structured error metadata for the brain layer to consume.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Resource-kind → official documentation URL mapping
# Extend this dict as new resource types are encountered in the wild.
# ---------------------------------------------------------------------------
DOCS_MAP: dict[str, str] = {
    "Deployment": "https://kubernetes.io/docs/concepts/workloads/controllers/deployment/",
    "StatefulSet": "https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/",
    "DaemonSet": "https://kubernetes.io/docs/concepts/workloads/controllers/daemonset/",
    "Service": "https://kubernetes.io/docs/concepts/services-networking/service/",
    "Ingress": "https://kubernetes.io/docs/concepts/services-networking/ingress/",
    "ConfigMap": "https://kubernetes.io/docs/concepts/configuration/configmap/",
    "Secret": "https://kubernetes.io/docs/concepts/configuration/secret/",
    "HorizontalPodAutoscaler": "https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/",
    "PersistentVolumeClaim": "https://kubernetes.io/docs/concepts/storage/persistent-volumes/",
    "CronJob": "https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/",
    "Job": "https://kubernetes.io/docs/concepts/workloads/controllers/job/",
    # Helm-level errors fall back to the Helm values docs
    "__helm__": "https://helm.sh/docs/chart_template_guide/values_files/",
    "__default__": "https://kubernetes.io/docs/concepts/overview/working-with-objects/",
}


@dataclass
class DeployResult:
    """Structured outcome of a helm deploy attempt."""

    success: bool
    stdout: str = ""
    stderr: str = ""
    return_code: int = 0
    error_kind: str = "__default__"
    doc_url: str = ""
    error_summary: str = ""
    faults: list[str] = field(default_factory=list)


def run_helm_deploy(
    chart_path: str,
    values_file: str,
    release_name: str = "local-release",
    namespace: str = "default",
    timeout: int = 120,
) -> DeployResult:
    """
    Attempt a `helm upgrade --install` against the active kubeconfig cluster.

    Returns a DeployResult with success flag, raw output streams, and
    pre-parsed error metadata ready for the brain layer.
    """
    cmd = [
        "helm", "upgrade", "--install",
        release_name, chart_path,
        "-f", values_file,
        "--namespace", namespace,
        "--create-namespace",
        "--timeout", f"{timeout}s",
        "--wait",
        "--atomic",          # rolls back automatically on failure
    ]

    print(f"[executor] Running: {' '.join(cmd)}")

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 30,
        )
    except subprocess.TimeoutExpired as exc:
        return DeployResult(
            success=False,
            stderr=f"Helm command timed out after {timeout + 30}s.",
            return_code=-1,
            error_kind="__default__",
            doc_url=DOCS_MAP["__default__"],
            error_summary="Deployment timed out — cluster may be unresponsive.",
            faults=["TimeoutExpired"],
        )
    except FileNotFoundError:
        return DeployResult(
            success=False,
            stderr="helm binary not found. Ensure Helm is installed and on PATH.",
            return_code=-2,
            error_kind="__default__",
            doc_url=DOCS_MAP["__default__"],
            error_summary="Helm binary missing from environment.",
            faults=["HelmNotFound"],
        )

    if proc.returncode == 0:
        return DeployResult(
            success=True,
            stdout=proc.stdout,
            stderr=proc.stderr,
            return_code=0,
        )

    # ── Failure path: parse the error log ──────────────────────────────────
    stderr_text = proc.stderr + proc.stdout  # helm sometimes writes to stdout
    kind = extract_error_kind(stderr_text)
    faults = extract_faults(stderr_text)

    return DeployResult(
        success=False,
        stdout=proc.stdout,
        stderr=stderr_text,
        return_code=proc.returncode,
        error_kind=kind,
        doc_url=lookup_doc_url(kind),
        error_summary=build_error_summary(stderr_text),
        faults=faults,
    )


# ---------------------------------------------------------------------------
# Error parsing helpers
# ---------------------------------------------------------------------------

def extract_error_kind(stderr: str) -> str:
    """
    Heuristically identify the Kubernetes resource Kind mentioned in the error.

    Order of precedence:
    1. Explicit "kind: X" in rendered YAML snippets inside the error.
    2. Resource type pattern like `deployments.apps` or `services`.
    3. Helm-level schema / values validation errors.
    4. Fall back to __default__.
    """
    # Pattern 1 — explicit kind field in error output
    kind_match = re.search(r'\bkind:\s*([A-Z][a-zA-Z]+)', stderr)
    if kind_match and kind_match.group(1) in DOCS_MAP:
        return kind_match.group(1)

    # Pattern 2 — Kubernetes API group resource references
    resource_match = re.search(
        r'\b(deployment|statefulset|daemonset|service|ingress|configmap|'
        r'secret|horizontalpodautoscaler|persistentvolumeclaim|cronjob|job)s?\b',
        stderr,
        re.IGNORECASE,
    )
    if resource_match:
        raw = resource_match.group(1).capitalize()
        # Normalise common pluralisations
        normalised = {
            "Horizontalpodautoscaler": "HorizontalPodAutoscaler",
            "Persistentvolumeclaim": "PersistentVolumeClaim",
            "Cronjob": "CronJob",
        }.get(raw, raw)
        if normalised in DOCS_MAP:
            return normalised

    # Pattern 3 — Helm values / schema error
    if any(kw in stderr.lower() for kw in ["values", "schema", "coerce", "invalid type"]):
        return "__helm__"

    return "__default__"


def extract_faults(stderr: str) -> list[str]:
    """
    Extract a list of human-readable fault descriptions from the error output.
    These are surfaced in the diagnostic report.
    """
    faults: list[str] = []

    patterns = [
        (r'invalid type.*?expected\s+(\w+)', "Type mismatch: expected {0}"),
        (r'cannot unmarshal.*?into\s+([\w.]+)', "Unmarshal error: {0}"),
        (r'(ImagePullBackOff|ErrImagePull)', "Image pull failure: {0}"),
        (r'(CrashLoopBackOff)', "Pod crash loop: {0}"),
        (r'invalid value\s+"([^"]+)"', 'Invalid value: "{0}"'),
        (r'field\s+(\w+)\s+not found', "Unknown field: {0}"),
        (r'(deprecated|removed)\s+in\s+(v[\d.]+)', "Deprecated API removed in {1}"),
        (r'exceeded its progress deadline', "Deployment progress deadline exceeded"),
    ]

    for pattern, template in patterns:
        for match in re.finditer(pattern, stderr, re.IGNORECASE):
            groups = match.groups()
            try:
                msg = template.format(*groups)
            except IndexError:
                msg = template
            if msg not in faults:
                faults.append(msg)

    if not faults:
        # Grab the first meaningful error line as a fallback
        for line in stderr.splitlines():
            line = line.strip()
            if line.lower().startswith("error") and len(line) > 10:
                faults.append(line[:200])
                break

    return faults


def lookup_doc_url(kind: str) -> str:
    """Return the canonical documentation URL for a given resource kind."""
    return DOCS_MAP.get(kind, DOCS_MAP["__default__"])


def build_error_summary(stderr: str) -> str:
    """Condense multi-line stderr into a single representative error string."""
    for line in stderr.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:500]
    return "Unknown deployment error."
