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
  - gemini-2.0-flash / gemini-2.0-flash-lite are deprecated June 1 2026.

Fallback behaviour:
  - If TINYFISH_API_KEY is absent or the fetch fails, a lightweight built-in
    HTML-to-text scraper is used so the pipeline never hard-blocks.
"""

from __future__ import annotations

import json
import os
import re
from typing import Optional

import requests
import google.generativeai as genai


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


# ──────────────────────────────────────────────────────────────────────────────
# Day 4 — TinyFish Context Compaction Layer
# ──────────────────────────────────────────────────────────────────────────────

def get_clean_context(documentation_url: str, tinyfish_key: Optional[str] = None) -> str:
    """
    Fetch a documentation URL and return clean, minimal Markdown.

    Primary path  : TinyFish Fetch API (strips DOM noise server-side).
    Fallback path : Direct requests call + lightweight regex HTML stripper,
                    used when no API key is configured or TinyFish is unreachable.

    Args:
        documentation_url: The canonical docs page to fetch.
        tinyfish_key:       TinyFish API key (optional; uses fallback if None).

    Returns:
        A Markdown string suitable for injection into a small-context LLM prompt.
    """
    if tinyfish_key:
        try:
            return _fetch_via_tinyfish(documentation_url, tinyfish_key)
        except Exception as exc:
            print(f"[brain] TinyFish fetch failed ({exc}); switching to fallback scraper.")

    return _fetch_via_fallback(documentation_url)


def _fetch_via_tinyfish(url: str, api_key: str) -> str:
    """Call the TinyFish Fetch API and return the Markdown payload."""
    # Auth uses X-API-Key header per official docs: https://docs.tinyfish.ai/fetch-api
    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json",
    }
    # POST body: urls array (batch-compatible endpoint)
    payload = {
        "urls": [url],
    }

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
                return _truncate_context(item[key], max_chars=8000)
    # Fallback: flat dict response
    if isinstance(data, dict):
        for key in ("markdown", "content", "text", "result"):
            if key in data and data[key]:
                return _truncate_context(data[key], max_chars=8000)

    return f"[TinyFish returned an unexpected response structure for {url}]"


def _fetch_via_fallback(url: str) -> str:
    """
    Lightweight fallback: fetch raw HTML and strip tags/boilerplate.
    Produces noisier output than TinyFish but keeps the pipeline alive.
    """
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": "GitOps-Horizon/1.0"})
        resp.raise_for_status()
        html = resp.text
    except Exception as exc:
        return f"[Fallback fetch failed for {url}: {exc}]"

    # Remove script/style/nav blocks entirely
    html = re.sub(r'<(script|style|nav|footer|header)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
    # Strip all remaining tags
    text = re.sub(r'<[^>]+>', ' ', html)
    # Collapse whitespace
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()

    return _truncate_context(text, max_chars=8000)


def _truncate_context(text: str, max_chars: int = 3500) -> str:
    """
    Ensure context stays within a safe token budget.
    3,500 chars ≈ ~900 tokens — well inside free-tier TPM limits.
    """
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n[...truncated...]"


# ──────────────────────────────────────────────────────────────────────────────
# Day 5 — Gemini Flash-Lite Remediation Engine
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
      2. Build a structured prompt with error log + docs + current YAML.
      3. Call Gemini Flash-Lite with JSON-mode output constraint.
      4. Parse and return the structured patch dict.

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

    # Step 2 — Build ultra-compact prompt (token budget: ~1,500 input tokens)
    # Truncate inputs defensively so the combined prompt stays well under free-tier TPM.
    error_snippet   = error_log.strip()[:1200]
    values_snippet  = current_values_yaml.strip()[:800]
    docs_snippet    = clean_docs[:1500]

    prompt = (
        "GitOps self-healing engine. Fix the Helm values file.\n\n"
        f"ERROR:\n{error_snippet}\n\n"
        f"CURRENT values (fix only broken fields):\n{values_snippet}\n\n"
        f"DOCS (reference):\n{docs_snippet}\n\n"
        "Return ONLY this JSON (no markdown fences):\n"
        '{"target_file":"config/cluster-values.yaml",'
        '"patched_values_block":"<full corrected YAML>",'
        '"fix_rationale":"<one sentence per fix>",'
        '"faults_resolved":["<fault1>"]}'
    )

    # Step 3 — Try model fallback chain: primary → fallback on 429
    override = model_name or os.getenv("GEMINI_MODEL")
    candidates = [override] if override else list(MODEL_FALLBACK_CHAIN)

    genai.configure(api_key=gemini_key)
    response = None
    last_exc: Exception | None = None

    for candidate in candidates:
        print(f"[brain] Calling {candidate} ...")
        try:
            mdl = genai.GenerativeModel(
                model_name=candidate,
                generation_config=genai.types.GenerationConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                    max_output_tokens=1024,
                ),
            )
            response = mdl.generate_content(prompt)
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

    # Step 4 — Parse JSON response
    raw_text = response.text.strip()

    # Strip accidental markdown fences if the model ignores the JSON-mode hint
    raw_text = re.sub(r'^```(?:json)?\s*', '', raw_text, flags=re.MULTILINE)
    raw_text = re.sub(r'```\s*$', '', raw_text, flags=re.MULTILINE)
    raw_text = raw_text.strip()

    try:
        patch = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Gemini returned non-JSON output. Raw response:\n{raw_text[:500]}"
        ) from exc

    _validate_patch(patch)
    return patch


def _validate_patch(patch: dict) -> None:
    """Raise ValueError if the patch is missing required fields."""
    required = {"target_file", "patched_values_block", "fix_rationale", "faults_resolved"}
    missing = required - set(patch.keys())
    if missing:
        raise ValueError(f"Gemini patch response missing fields: {missing}. Got: {list(patch.keys())}")
    if not isinstance(patch.get("patched_values_block"), str):
        raise ValueError("patched_values_block must be a string of YAML content.")
