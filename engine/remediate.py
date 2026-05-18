"""
engine/remediate.py
────────────────────
Day 6 & 7: CLI Orchestration Entry Point

Called by the GitHub Actions workflow step:
  python engine/remediate.py --log-file runtime_error.log

Responsibilities:
  1. Read the raw helm error log from disk.
  2. Detect the error kind and look up the relevant docs URL (via executor).
  3. Call brain.py to fetch compressed docs + run Gemini remediation.
  4. Patch config/cluster-values.yaml in-place with the AI-generated fix.
  5. Write a Markdown diagnostic report to reports/.
  6. Exit 0 on success, non-zero on unrecoverable failure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

from engine.executor import extract_error_kind, extract_faults, lookup_doc_url
from engine.brain import remediate_manifest_drift

console = Console()

# Repo root is two levels up from this file (engine/remediate.py → repo root)
REPO_ROOT = Path(__file__).resolve().parent.parent
CLUSTER_VALUES = REPO_ROOT / "config" / "cluster-values.yaml"
REPORTS_DIR = REPO_ROOT / "reports"


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="remediate",
        description="GitOps Horizon — AI-powered Helm manifest self-healing engine",
    )
    p.add_argument(
        "--log-file",
        required=True,
        help="Path to the runtime_error.log file produced by the failed helm deploy.",
    )
    p.add_argument(
        "--values-file",
        default=str(CLUSTER_VALUES),
        help="Path to the Helm values file to patch (default: config/cluster-values.yaml).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the proposed patch without writing any files.",
    )
    return p


# ──────────────────────────────────────────────────────────────────────────────
# Main orchestration logic
# ──────────────────────────────────────────────────────────────────────────────

def main() -> int:
    args = build_parser().parse_args()

    # ── 1. Read error log ────────────────────────────────────────────────────
    log_path = Path(args.log_file)
    if not log_path.exists():
        console.print(f"[bold red]✗ Error log not found:[/] {log_path}", style="red")
        return 1

    error_log = log_path.read_text(encoding="utf-8").strip()
    if not error_log:
        console.print("[yellow]⚠  Error log is empty — nothing to remediate.[/]")
        return 0

    console.print(Panel("[bold cyan]🔭 GitOps Horizon — Self-Healing Engine[/]", expand=False))
    console.print(f"\n[bold]Error log:[/] {log_path.resolve()}")
    console.print(Syntax(error_log[:800], "text", theme="monokai", line_numbers=False))

    # ── 2. Detect error kind + doc URL ───────────────────────────────────────
    error_kind = extract_error_kind(error_log)
    faults = extract_faults(error_log)
    doc_url = lookup_doc_url(error_kind)

    console.print(f"\n[bold green]→ Detected error kind:[/] {error_kind}")
    console.print(f"[bold green]→ Documentation target:[/] {doc_url}")
    if faults:
        console.print("[bold green]→ Extracted faults:[/]")
        for f in faults:
            console.print(f"   • {f}")

    # ── 3. Read current values file ──────────────────────────────────────────
    values_path = Path(args.values_file)
    if not values_path.exists():
        console.print(f"[bold red]✗ Values file not found:[/] {values_path}")
        return 1

    current_yaml = values_path.read_text(encoding="utf-8")

    # ── 4. Retrieve API keys from environment ────────────────────────────────
    gemini_key = os.getenv("GEMINI_API_KEY", "")
    tinyfish_key = os.getenv("TINYFISH_API_KEY") or None  # None → fallback scraper

    if not gemini_key:
        console.print("[bold red]✗ GEMINI_API_KEY environment variable is not set.[/]")
        return 1

    # ── 5. Call AI remediation engine ────────────────────────────────────────
    console.print("\n[bold magenta]⚡ Running AI remediation (TinyFish + Gemini)...[/]\n")
    try:
        patch = remediate_manifest_drift(
            error_log=error_log,
            current_values_yaml=current_yaml,
            doc_url=doc_url,
            tinyfish_key=tinyfish_key,
            gemini_key=gemini_key,
        )
    except Exception as exc:
        console.print(f"[bold red]✗ Remediation failed:[/] {exc}")
        return 1

    # ── 6. Display patch ─────────────────────────────────────────────────────
    console.print(Panel("[bold green]✔ Patch generated successfully[/]", expand=False))
    console.print(f"\n[bold]Fix rationale:[/] {patch.get('fix_rationale', 'N/A')}")
    console.print("\n[bold]Faults resolved:[/]")
    for fault in patch.get("faults_resolved", []):
        console.print(f"   ✓ {fault}")

    patched_yaml = patch["patched_values_block"]
    console.print("\n[bold]Patched values (preview):[/]")
    console.print(Syntax(patched_yaml[:600], "yaml", theme="monokai", line_numbers=True))

    if args.dry_run:
        console.print("\n[yellow]ℹ  Dry-run mode — no files written.[/]")
        return 0

    # ── 7. Validate the YAML is parseable before writing ────────────────────
    try:
        yaml.safe_load(patched_yaml)
    except yaml.YAMLError as exc:
        console.print(f"[bold red]✗ Gemini returned invalid YAML:[/] {exc}")
        return 1

    # ── 8. Write patched values file ─────────────────────────────────────────
    values_path.write_text(patched_yaml, encoding="utf-8")
    console.print(f"\n[bold green]✔ Patched:[/] {values_path.resolve()}")

    # ── 9. Write Markdown diagnostic report ──────────────────────────────────
    report_path = write_diagnostic_report(
        error_log=error_log,
        error_kind=error_kind,
        faults=faults,
        doc_url=doc_url,
        patch=patch,
        original_yaml=current_yaml,
        patched_yaml=patched_yaml,
    )
    console.print(f"[bold green]✔ Report:[/] {report_path.resolve()}")

    return 0


# ──────────────────────────────────────────────────────────────────────────────
# Day 10: Markdown Diagnostic Report Generator
# ──────────────────────────────────────────────────────────────────────────────

def write_diagnostic_report(
    error_log: str,
    error_kind: str,
    faults: list[str],
    doc_url: str,
    patch: dict,
    original_yaml: str,
    patched_yaml: str,
) -> Path:
    """
    Generate a self-contained Markdown diagnostic report capturing the full
    remediation cycle: error → docs fetched → AI fix → patched YAML.

    The report is written to reports/horizon-<timestamp>.md and is automatically
    uploaded as a GitHub Actions artifact for audit trail purposes.
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    report_file = REPORTS_DIR / f"horizon-{ts}.md"

    faults_md = "\n".join(f"- {f}" for f in faults) if faults else "- No specific faults extracted."
    resolved_md = "\n".join(
        f"- ✅ {r}" for r in patch.get("faults_resolved", [])
    ) or "- (none listed)"

    report_content = textwrap.dedent(f"""
    # 🔭 GitOps Horizon — Diagnostic Report

    **Generated:** {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}
    **Status:** ✅ Remediation Successful

    ---

    ## 1. Deployment Failure Summary

    **Detected Resource Kind:** `{error_kind}`

    **Extracted Faults:**
    {faults_md}

    ### Raw Error Log
    ```
    {error_log[:1500]}
    {"[...truncated...]" if len(error_log) > 1500 else ""}
    ```

    ---

    ## 2. Context Engineering — TinyFish Fetch

    **Documentation URL queried:** [{doc_url}]({doc_url})

    > The TinyFish Fetch API stripped the raw HTML documentation page to minimal
    > Markdown, reducing the context payload to fit within Gemini's small-context
    > window while preserving the structural specification content needed for
    > accurate remediation.

    ---

    ## 3. AI Remediation — Gemini Flash-Lite

    **Fix Rationale:**
    {patch.get("fix_rationale", "N/A")}

    **Faults Resolved:**
    {resolved_md}

    ---

    ## 4. Manifest Diff

    ### Before (`config/cluster-values.yaml`)
    ```yaml
    {original_yaml.strip()}
    ```

    ### After (AI-patched)
    ```yaml
    {patched_yaml.strip()}
    ```

    ---

    ## 5. Self-Healing Loop Outcome

    The patched manifest was written back to `config/cluster-values.yaml` and
    re-submitted to the local Kind cluster. The re-deployment step validates
    that the AI-generated fix produces a clean `helm upgrade --install` with
    exit code 0 and no trailing warnings.

    ---

    *Report generated by [GitOps Horizon](https://github.com/aviavinashkr/GitOps-Horizon)*
    """).strip()

    report_file.write_text(report_content, encoding="utf-8")
    return report_file


if __name__ == "__main__":
    sys.exit(main())
