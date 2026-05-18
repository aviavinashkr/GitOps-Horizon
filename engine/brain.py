"""
engine/brain.py
───────────────
Days 4 & 5: TinyFish Context Compaction + Gemini Remediation

Architecture:
  1. TinyFish Fetch API strips raw HTML documentation pages to clean Markdown,
     keeping the context payload tiny enough for free-tier models.
  2. Gemini 2.5 Flash-Lite (free tier: 1,000 RPD) receives the condensed docs
     + the helm error log and returns a strict JSON patch for the values file.

Model strategy:
  - Default: gemini-2.5-flash-lite  (1,000 RPD free — most quota headroom)
  - Fallback: gemini-2.5-flash      (250 RPD free)
  - Override via GEMINI_MODEL env var at any time.
  - gemini-2.0-* series are deprecated/removed from free tier June 2026.

SDK: google-genai (pip: google-genai>=1.0.0)
  - google.generativeai is fully end-of-life — do not import it.

Fallback behaviour:
  - If TINYFISH_API_KEY is absent or the fetch fails, a lightweight built-in
    HTML-to-text scraper is used so the pipeline never hard-blocks.
  - If docs fetch returns < 200 chars (JS-rendered page, bot block, etc.)
    the model falls back to its built-in Kubernetes knowledge.
"""

from __future__ import annotations

import json
import os
import re
from typing import Optional

import requests
from google import genai
from google.genai import types as genai_types


# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

TINYFISH_ENDPOINT = "https://api.fetch.tinyfish.ai"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash-lite"   # 1,000 RPD on free tier
REQUEST_TIMEOUT = 30  # seconds

# Ordered fallback chain — tried in sequence if the primary model 429s
MODEL_FALLBACK_CHAIN = [
    "gemini-2.5-flash-lite",   # 1,000 RPD — try first
    "gemini-2.5-flash",        # 250 RPD  — fallback
]

# Minimum useful context length; below this we skip docs and rely on model knowledge
MIN_CONTEXT_CHARS = 200


# ──────────────────────────────────────────────────────────────────────────────
# Day 4 — TinyFish Context Compaction Layer
# ──────────────────────────────────────────────────────────────────────────────

def get_clean_context(documentation_url: str, tinyfish_key: Optional[str] = None) -> str:
    """
    Fetch a documentation URL and return clean, minimal Markdown.

    Primary path  : TinyFish Fetch API (strips DOM noise server-side).
    Fallback path : Direct requests call + lightweight regex HTML stripper.
                    If the result is still too short (JS-rendered page),
                    returns an empty string so the caller falls back to
                    the model's built-in knowledge.

    Args:
        documentation_url: The canonical docs page to fetch.
        tinyfish_key:       TinyFish API key (optional; uses fallback if None).

    Returns:
        A Markdown string, or "" if no useful content could be retrieved.
    """
    if tinyfish_key:
        try:
            result = _fetch_via_tinyfish(documentation_url, tinyfish_key)
            if len(result) >= MIN_CONTEXT_CHARS:
                return result
            print(f"[brain] TinyFish returned only {len(result)} chars — insufficient.")
        except Exception as exc:
            print(f"[brain] TinyFish fetch failed ({exc}); switching to fallback scraper.")

    result = _fetch_via_fallback(documentation_url)
    if len(result) < MIN_CONTEXT_CHARS:
        print(f"[brain] Fallback scraper returned only {len(result)} chars (JS-rendered?) "
              "— will rely on model built-in knowledge.")
        return ""
    return result


def _fetch_via_tinyfish(url: str, api_key: str) -> str:
    """Call the TinyFish Fetch API and return the Markdown payload."""
    # Auth uses X-API-Key header per official docs: https://docs.tinyfish.ai/fetch-api
    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json",
    }
    # POST body: urls array (batch-compatible endpoint)
    payload = {"urls": [url]}

    response = requests.post(
        TINYFISH_ENDPOINT,
        headers=headers,
        json=payload,
        timeout=150,  # Fetch API has 110s per-URL backend timeout; use 150s client timeout
    )
    response.raise_for_status()
    data = response.json()

    # Response is a list of results when urls array is passed
    if isinstance(data, list) and data:
        item = data[0]
        for key in ("markdown", "content", "text", "result"):
            if key in item and item[key]:
                return _truncate_context(item[key], max_chars=3000)
    # Fallback: flat dict response
    if isinstance(data, dict):
        for key in ("markdown", "content", "text", "result"):
            if key in data and data[key]:
                return _truncate_context(data[key], max_chars=3000)

    return ""


def _fetch_via_fallback(url: str) -> str:
    """
    Lightweight fallback: fetch raw HTML and strip tags/boilerplate.
    kubernetes.io is JS-rendered so this often returns near-nothing —
    callers must check len() before using the result.
    """
    try:
        resp = requests.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; GitOps-Horizon/1.0)"},
        )
        resp.raise_for_status()
        html = resp.text
    except Exception as exc:
        return f"[Fallback fetch failed for {url}: {exc}]"

    # Remove script/style/nav blocks entirely
    html = re.sub(r'<(script|style|nav|footer|header)[^>]*>.*?</\1>', '', html,
                  flags=re.DOTALL | re.IGNORECASE)
    # Strip all remaining tags
    text = re.sub(r'<[^>]+>', ' ', html)
    # Collapse whitespace
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return _truncate_context(text.strip(), max_chars=3000)


def _truncate_context(text: str, max_chars: int = 3000) -> str:
    """Ensure context stays within a safe token budget (~750 tokens at 3000 chars)."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n[...truncated...]"


def _strip_yaml_comments(yaml_text: str) -> str:
    """
    Remove # comment lines from YAML before sending to the model.

    This prevents the model from faithfully reproducing long comment blocks
    (like '# INTENTIONALLY BROKEN') verbatim in its output, which was causing
    max_output_tokens to be exhausted on comment reproduction rather than fixes.
    """
    lines = yaml_text.splitlines()
    stripped = [ln for ln in lines if not ln.strip().startswith("#") and ln.strip()]
    return "\n".join(stripped)


# ──────────────────────────────────────────────────────────────────────────────
# Day 5 — Gemini Remediation Engine (google-genai SDK)
# ──────────────────────────────────────────────────────────────────────────────

def remediate_manifest_drift(
    error_log: str,
    current_values_yaml: str,
    doc_url: str,
    tinyfish_key: Optional[str],
    gemini_key: str,
    model_name: Optional[str] = None,
) -> dict:
    """
    Core AI remediation function.

    Steps:
      1. Fetch condensed documentation context via TinyFish (or fallback).
      2. Strip YAML comments before sending (prevents verbatim reproduction).
      3. Build a compact prompt and call Gemini via the google-genai SDK.
      4. Parse and return the structured JSON patch dict.

    Args:
        error_log:            Raw stderr from the failed helm deploy.
        current_values_yaml:  Content of config/cluster-values.yaml as a string.
        doc_url:              Documentation URL relevant to the detected error kind.
        tinyfish_key:         TinyFish API key (None → fallback scraper).
        gemini_key:           Google Gemini API key.
        model_name:           Override the Gemini model (default: gemini-2.5-flash-lite).

    Returns:
        A dict with keys: target_file, patched_values_block, fix_rationale, faults_resolved.
    """
    # Step 1 — Compress documentation context
    print(f"[brain] Fetching condensed docs from: {doc_url}")
    clean_docs = get_clean_context(doc_url, tinyfish_key)
    print(f"[brain] Context size: {len(clean_docs)} chars")

    # Step 2 — Strip YAML comments so the model doesn't reproduce them verbatim,
    # then cap each section to keep total input well under free-tier TPM.
    error_snippet  = error_log.strip()[:1000]
    values_clean   = _strip_yaml_comments(current_values_yaml)[:600]
    docs_snippet   = clean_docs[:1500] if clean_docs else "(none — use built-in K8s knowledge)"

    # Step 3 — Compact, explicit prompt
    prompt = (
        "You are a GitOps self-healing engine. A Helm deployment failed.\n"
        "Fix the cluster-values.yaml. Output ONLY the corrected YAML (no comments, no markdown).\n\n"
        f"DEPLOYMENT ERROR:\n{error_snippet}\n\n"
        f"CURRENT values (comments stripped):\n{values_clean}\n\n"
        f"REFERENCE DOCS:\n{docs_snippet}\n\n"
        "Rules:\n"
        "1. Fix every fault shown in the error. Change ONLY broken fields.\n"
        "2. replicaCount must be an integer.\n"
        "3. image.tag must be a real, publicly-pullable tag (use '1.25.3').\n"
        "4. resources.limits.cpu must be a valid K8s quantity (e.g. '100m').\n"
        "5. Output ONLY this JSON object — no markdown fences, no extra text:\n"
        '{"target_file":"config/cluster-values.yaml",'
        '"patched_values_block":"<complete corrected YAML, no comments>",'
        '"fix_rationale":"<one sentence per fix>",'
        '"faults_resolved":["<fault1>","<fault2>"]}'
    )

    # Step 4 — Initialise the new google-genai client
    client = genai.Client(api_key=gemini_key)

    override = model_name or os.getenv("GEMINI_MODEL")
    candidates = [override] if override else list(MODEL_FALLBACK_CHAIN)

    response = None
    last_exc: Exception | None = None

    for candidate in candidates:
        print(f"[brain] Calling {candidate} ...")
        try:
            response = client.models.generate_content(
                model=candidate,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                    max_output_tokens=2048,   # output budget: full YAML + JSON wrapper
                ),
            )
            print(f"[brain] ✓ Response received from {candidate}")
            break
        except Exception as exc:
            last_exc = exc
            if "429" in str(exc) or "quota" in str(exc).lower():
                print(f"[brain] {candidate} quota hit — trying next model in chain.")
                continue
            raise  # non-quota errors bubble immediately

    if response is None:
        raise RuntimeError(
            f"All models in fallback chain exhausted. Last error: {last_exc}"
        )

    # Step 5 — Parse JSON response
    raw_text = response.text.strip()

    # Strip accidental markdown fences if the model ignores the JSON-mode hint
    raw_text = re.sub(r'^```(?:json)?\s*', '', raw_text, flags=re.MULTILINE)
    raw_text = re.sub(r'```\s*$', '', raw_text, flags=re.MULTILINE)
    raw_text = raw_text.strip()

    try:
        patch = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        # Provide a diagnostic dump to help debug future truncation issues
        raise ValueError(
            f"Gemini returned non-JSON output (len={len(raw_text)}).\n"
            f"First 600 chars:\n{raw_text[:600]}\n"
            f"Last 200 chars:\n{raw_text[-200:]}"
        ) from exc

    _validate_patch(patch)
    return patch


def _validate_patch(patch: dict) -> None:
    """Raise ValueError if the patch is missing required fields."""
    required = {"target_file", "patched_values_block", "fix_rationale", "faults_resolved"}
    missing = required - set(patch.keys())
    if missing:
        raise ValueError(
            f"Gemini patch response missing fields: {missing}. Got: {list(patch.keys())}"
        )
    if not isinstance(patch.get("patched_values_block"), str):
        raise ValueError("patched_values_block must be a string of YAML content.")
