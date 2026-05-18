# 🔭 GitOps Horizon

> **Automated Helm Runtime Mutation & Self-Healing Engine**
>
> A portfolio-grade GitOps system that deploys a live ephemeral Kubernetes cluster
> inside GitHub Actions, detects Helm manifest drift at runtime, fetches compressed
> documentation via **TinyFish**, and auto-remediates failures using **Gemini Flash-Lite** — all
> at **zero cloud cost**.

![GitHub Actions](https://img.shields.io/github/actions/workflow/status/aviavinashkr/GitOps-Horizon/horizon-engine.yml?label=Horizon%20Engine&logo=github-actions&style=for-the-badge)
![License](https://img.shields.io/github/license/aviavinashkr/GitOps-Horizon?style=for-the-badge)
![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)
![Kubernetes](https://img.shields.io/badge/Kind-Kubernetes-326CE5?style=for-the-badge&logo=kubernetes&logoColor=white)
![Gemini](https://img.shields.io/badge/Gemini-Flash--Lite-4285F4?style=for-the-badge&logo=google&logoColor=white)

---

## Architecture

```
[Developer PR / git push]
         │
         ▼
[GitHub Actions Runner — ubuntu-latest]
         │
         ├─► [Step 1] helm lint ./charts/sample-app   (pre-flight)
         │
         ├─► [Step 2] Kind Cluster instantiated locally (free, ephemeral, Docker-backed)
         │
         ├─► [Step 3] helm upgrade --install  ─────────────────────────────┐
         │               ↓ (failure)                                        │
         │           runtime_error.log captured                             │
         │               │                                                  │
         │   ┌───────────▼────────────────────────────────────────────┐    │
         │   │  engine/remediate.py                                   │    │
         │   │  ├── executor.py  → parse error kind, look up docs URL │    │
         │   │  ├── brain.py     → TinyFish fetch (minimal Markdown)  │    │
         │   │  │                → Gemini Flash-Lite (JSON patch)     │    │
         │   │  └── patcher      → config/cluster-values.yaml mutated │    │
         │   └───────────────────────────────────────────────────────-┘    │
         │               │                                                  │
         ├─► [Step 4] Re-deploy with patched values ◄─────────────────────-┘
         │               ↓ (exit 0)
         ├─► [Step 5] Markdown diagnostic report generated → reports/
         │
         └─► [Step 6] git commit + push patched files back to PR branch
```

---

## Key Design Principles

| Principle | How GitOps Horizon Implements It |
|---|---|
| **Zero cloud cost** | Kind cluster runs entirely inside the free GitHub Actions container — no cloud account needed |
| **Context engineering** | TinyFish strips HTML documentation to minimal Markdown before feeding it to the LLM |
| **Dynamic validation** | Failures detected by actually running `helm upgrade`, not static linting |
| **Self-healing loop** | After patching, the same Kind cluster re-runs the deployment to confirm the fix |
| **Audit trail** | Every remediation cycle produces a timestamped Markdown report committed to the repo |

---

## Repository Structure

```
GitOps-Horizon/
├── .github/
│   └── workflows/
│       └── horizon-engine.yml      # 12-step self-healing CI pipeline
│
├── charts/
│   └── sample-app/                 # Standard Helm chart (NGINX)
│       ├── Chart.yaml
│       ├── values.yaml             # Correct default values
│       └── templates/
│           ├── _helpers.tpl
│           ├── deployment.yaml
│           └── service.yaml
│
├── config/
│   └── cluster-values.yaml         # ⚠️  Intentionally broken (3 synthetic faults)
│
├── engine/
│   ├── __init__.py
│   ├── executor.py                 # Day 3: subprocess Helm wrapper + error parser
│   ├── brain.py                    # Days 4-5: TinyFish fetch + Gemini remediation
│   └── remediate.py               # Days 6-7: CLI entry + YAML patcher + report writer
│
├── reports/                        # Auto-generated diagnostic Markdown files
├── requirements.txt
└── README.md
```

---

## The Intentional Faults (Day 9: Exception Engineering)

`config/cluster-values.yaml` ships with **3 synthetic faults** that produce realistic production-class errors:

| # | Fault | Type | Trigger |
|---|---|---|---|
| 1 | `replicaCount: "two"` | Type mismatch | Helm/K8s expects integer, gets string |
| 2 | `image.tag: "99.99.99-nonexistent"` | ImagePullBackOff | Tag doesn't exist on Docker Hub |
| 3 | `resources.limits.cpu: "0xBAD"` | Invalid K8s quantity | Kubernetes quantity parser rejects hex |

The engine detects which faults occurred, fetches the relevant docs page, and produces corrected YAML targeting only the broken fields.

---

## Engine Components

### `engine/executor.py` — Runtime Execution Engine

```python
result = run_helm_deploy(
    chart_path="./charts/sample-app",
    values_file="./config/cluster-values.yaml",
)
# result.success        → False (broken config)
# result.error_kind     → "Deployment"
# result.doc_url        → "https://kubernetes.io/docs/..."
# result.faults         → ["Type mismatch: expected int", ...]
```

- Wraps `helm upgrade --install` via `subprocess`
- Separates stdout/stderr and maps return codes
- Extracts the failing Kubernetes resource Kind via regex
- Maps Kinds to canonical documentation URLs

### `engine/brain.py` — TinyFish + Gemini Layer

```python
patch = remediate_manifest_drift(
    error_log=result.stderr,
    current_values_yaml=open("config/cluster-values.yaml").read(),
    doc_url=result.doc_url,
    tinyfish_key=os.environ["TINYFISH_API_KEY"],
    gemini_key=os.environ["GEMINI_API_KEY"],
)
# patch["patched_values_block"] → corrected YAML string
# patch["fix_rationale"]        → human-readable explanation
# patch["faults_resolved"]      → list of fixed issues
```

**TinyFish fallback**: if `TINYFISH_API_KEY` is absent, a built-in lightweight HTML-to-text scraper activates automatically — the pipeline never hard-blocks.

### `engine/remediate.py` — CLI Orchestrator

```bash
# Used by the GitHub Actions workflow
python -m engine.remediate --log-file runtime_error.log

# Local dry-run (no files written)
python -m engine.remediate --log-file runtime_error.log --dry-run
```

---

## Setup & Configuration

### 1. Repository Secrets

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Description |
|---|---|
| `GEMINI_API_KEY` | Google AI Studio API key — [get one free](https://aistudio.google.com/apikey) |
| `TINYFISH_API_KEY` | TinyFish API key (optional — pipeline falls back to direct scraper if absent) |

### 2. Triggering the Self-Healing Loop

Open a pull request that touches `charts/`, `config/`, or `engine/`. The workflow fires automatically. The broken `config/cluster-values.yaml` will cause the initial deploy to fail, triggering the full remediation cycle.

**Manual trigger** via GitHub Actions UI (`workflow_dispatch`) is also supported for demos.

### 3. Local Development

```bash
# Clone and install
git clone https://github.com/aviavinashkr/GitOps-Horizon.git
cd GitOps-Horizon
pip install -r requirements.txt

# Run the remediation engine locally (dry-run, no file writes)
export GEMINI_API_KEY="your-key-here"
echo "Error: UPGRADE FAILED: invalid type for replicaCount" > test_error.log
python -m engine.remediate --log-file test_error.log --dry-run
```

---

## GitHub Actions Workflow — 12-Step Pipeline

| Step | Name | Condition |
|---|---|---|
| 1 | 📥 Checkout Repository | Always |
| 2 | ☸️ Instantiate Free Local Kind Cluster | Always |
| 3 | 🪖 Install Helm | Always |
| 4 | 🩺 Validate Cluster Health + Lint | Always |
| 5 | 🚀 Execute Deployment (captures failure) | Always |
| 6 | 📋 Upload Raw Error Log | `DEPLOY_FAILED == true` |
| 7 | 🐍 Install Engine Dependencies | `DEPLOY_FAILED == true` |
| 8 | 🤖 Run Dynamic Adaptation & Healing Loop | `DEPLOY_FAILED == true` |
| 9 | ✅ Re-Verify Patched Manifest on Kind | `DEPLOY_FAILED == true` |
| 10 | 📊 Upload Diagnostic Report Artifact | `DEPLOY_FAILED == true` |
| 11 | 💾 Commit Remediated Assets to PR Branch | `DEPLOY_FAILED == true` |
| 12 | 📝 Pipeline Summary (GITHUB_STEP_SUMMARY) | Always |

---

## Diagnostic Report (Day 10)

Every successful remediation writes a timestamped Markdown report to `reports/horizon-<YYYYMMDD-HHMMSS>.md` with:

- **Failure summary** — error log + extracted faults
- **Context engineering trace** — TinyFish URL queried + characters fetched
- **AI reasoning** — fix rationale from Gemini
- **Before/after diff** — original vs. patched YAML
- **Validation result** — re-deploy exit code

Reports are also uploaded as GitHub Actions artifacts (retained 30 days).

---

## Why This Showcases Real Engineering

| Portfolio Signal | Implementation Evidence |
|---|---|
| Infrastructure-as-Code at scale | Full Helm chart with parameterised templates, helper partials, probes |
| Context engineering | TinyFish reduces 80KB+ HTML documentation to ~8KB structured Markdown |
| Production-realistic failure modes | ImagePullBackOff, type mismatch, invalid K8s quantity — not toy errors |
| Self-validating AI outputs | Patch is YAML-parsed before file write; re-deploy confirms fix empirically |
| Enterprise CI/CD patterns | Atomic Helm releases, rollback, concurrency guards, artifact retention |
| Zero-cost cloud simulation | Kind on ubuntu-latest runner replicates a real GKE/EKS control plane locally |

---

## Technology Stack

- **Kubernetes-in-Docker (Kind)** — ephemeral local cluster on the GitHub runner
- **Helm 3** — package manager for Kubernetes manifests
- **TinyFish Fetch API** — server-side HTML-to-Markdown context compressor
- **Google Gemini 2.0 Flash-Lite** — low-latency, free-tier AI model for YAML remediation
- **Python 3.11+** — orchestration engine (`subprocess`, `requests`, `pyyaml`, `rich`)
- **GitHub Actions** — CI/CD pipeline host

---

## License

MIT © [aviavinashkr](https://github.com/aviavinashkr)
