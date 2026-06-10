"""
Cross-video synthesis — compounding, category-paced, schema-driven aggregation.

Two composable steps so the agent/CLI can run a confirm loop:

  categorize(source)        — resolve + ONE cheap title-classification pass. Discovers
                              the category set and assigns every video. Read-only, no
                              extraction. Cached per source. This is what you show the
                              user to confirm before spending on extraction.

  synthesize_pass(source, category, item_schema, aggregate_schema, top_n=...)
                            — select top-N of one category → extract_structured over
                              them → fold the results into a PERSISTED, resumable
                              aggregate for (source, aggregate_schema). Each pass
                              compounds onto the last.

Both the classify and the aggregate steps reason over text (titles; per-video JSON),
not video — so they use gemini_selector._call_gemini_text. Validation/retry reuse
structured_extractor.validate_output. The per-(video, schema) extraction cache makes
re-runs free.
"""

import hashlib
import json
from pathlib import Path

import screenscribe.session as _session
from screenscribe.config import GEMINI_MODEL, GEMINI_MEDIA_RESOLUTION_LOW
from screenscribe.structured_extractor import (
    extract_structured,
    resolve_schema,
    validate_output,
)

_AGG_PRESET_DIR = Path(__file__).parent / "schemas" / "aggregate"

_CATEGORIZE_SCHEMA = {
    "type": "object",
    "properties": {
        "categories": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The small set of natural dish categories discovered from the titles",
        },
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "video_id": {"type": "string"},
                    "category": {"type": "string"},
                },
                "required": ["video_id", "category"],
            },
        },
    },
    "required": ["categories", "assignments"],
}


# ── paths / keys ─────────────────────────────────────────────────────────────

def _synth_root() -> Path:
    return _session.SESSIONS_DIR / "synthesis"


def _source_key(source) -> str:
    canonical = json.dumps(source, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def _categories_path(source) -> Path:
    return _synth_root() / _source_key(source) / "categories.json"


def _aggregate_key(aggregate_schema) -> str:
    """Stable key for an aggregate schema (preset name as-is, else hash of the resolved schema)."""
    if isinstance(aggregate_schema, str) and (_AGG_PRESET_DIR / f"{aggregate_schema}.json").exists():
        return aggregate_schema
    schema = resolve_aggregate_schema(aggregate_schema)
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def _aggregate_path(source, aggregate_schema) -> Path:
    key = f"{_source_key(source)}__{_aggregate_key(aggregate_schema)}"
    return _synth_root() / key / "aggregate.json"


def resolve_aggregate_schema(arg) -> dict:
    """Resolve an aggregate schema: dict | aggregate-preset name | file path | inline JSON."""
    if isinstance(arg, str):
        preset = _AGG_PRESET_DIR / f"{arg}.json"
        if preset.exists():
            return json.loads(preset.read_text())
    return resolve_schema(arg)  # dict / file path / inline JSON


def list_aggregate_presets() -> list[str]:
    return sorted(p.stem for p in _AGG_PRESET_DIR.glob("*.json"))


# ── categorize ───────────────────────────────────────────────────────────────

def _build_categorize_prompt(titled: list[dict]) -> str:
    lines = "\n".join(f'{s["id"]}\t{s["title"]}' for s in titled)
    return (
        "Below are YouTube video titles (one per line as `video_id<TAB>title`), which may "
        "be in any language. Group them into a SMALL set of natural dish categories "
        "(e.g. vegetarian, fish, chicken, egg, sweets, …) inferred from the titles, and "
        "assign EVERY video to exactly one category. Keep category names short and "
        "lowercase. Return JSON matching the schema.\n\n"
        f"TITLES:\n{lines}"
    )


def categorize(source, *, max_videos=None, min_duration=0, force=False, model=GEMINI_MODEL) -> dict:
    """Resolve `source` and classify its videos into discovered categories from titles.
    Read-only and cheap; cached per source. Returns:
      {status, source, source_key, kind, categories:[{name,count,video_ids}],
       by_id:{video_id:category}, ranked:bool, total}
    """
    from screenscribe.resolver import resolve_videos
    from screenscribe.gemini_selector import _call_gemini_text, gemini_available

    path = _categories_path(source)
    if path.exists() and not force:
        return json.loads(path.read_text())

    resolved = resolve_videos(source, max_videos=max_videos, min_duration=min_duration)
    stubs = resolved["videos"]
    titled = [s for s in stubs if s.get("title")]

    if not titled:
        return {"status": "error",
                "error": "No video titles available to classify (channel/playlist input needed)."}
    if not gemini_available():
        return {"status": "error", "error": "categorize needs GEMINI_API_KEY."}

    prompt = _build_categorize_prompt(titled)
    raw = _call_gemini_text(model, prompt, _CATEGORIZE_SCHEMA)
    ok, data, err = validate_output(raw, _CATEGORIZE_SCHEMA)
    if not ok:
        raw = _call_gemini_text(
            model, prompt + f"\nYour previous output failed validation: {err}\nReturn corrected JSON.",
            _CATEGORIZE_SCHEMA,
        )
        ok, data, err = validate_output(raw, _CATEGORIZE_SCHEMA)
    if not ok:
        return {"status": "error", "error": f"categorize output invalid: {err}", "raw": raw}

    valid_ids = {s["id"] for s in stubs}
    by_id = {a["video_id"]: a["category"] for a in data["assignments"] if a["video_id"] in valid_ids}
    for s in stubs:                                   # never silently drop: unassigned → uncategorized
        by_id.setdefault(s["id"], "uncategorized")

    views = {s["id"]: s.get("view_count") for s in stubs}
    ranked = any(v is not None for v in views.values())

    buckets: dict[str, list[str]] = {}
    for s in stubs:
        buckets.setdefault(by_id[s["id"]], []).append(s["id"])

    categories = []
    for name, ids in buckets.items():
        if ranked:
            ids = sorted(ids, key=lambda i: (views[i] is not None, views[i] or 0), reverse=True)
        categories.append({"name": name, "count": len(ids), "video_ids": ids})
    categories.sort(key=lambda c: c["count"], reverse=True)

    result = {
        "status": "success", "source": source, "source_key": _source_key(source),
        "kind": resolved["kind"], "categories": categories, "by_id": by_id,
        "ranked": ranked, "total": len(stubs),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2))
    return result


# ── synthesize_pass (compounding) ─────────────────────────────────────────────

def _build_aggregate_prompt(prior, new_results, category, aggregate_schema) -> str:
    desc = aggregate_schema.get("description", "")
    return (
        "You are incrementally building one aggregate artifact from many videos, conforming "
        "to the provided JSON schema. Integrate the NEW per-video results below into the "
        "EXISTING aggregate: add them under the matching category, deduplicate by dish/name, "
        "keep it coherent, and preserve everything already in the aggregate. Return the FULL "
        "updated aggregate as JSON.\n"
        f"{('Aggregate purpose: ' + desc) if desc else ''}\n\n"
        f"CATEGORY OF THIS BATCH: {category}\n\n"
        f"EXISTING AGGREGATE (may be null on the first pass):\n{json.dumps(prior)}\n\n"
        f"NEW PER-VIDEO RESULTS:\n{json.dumps(new_results)}"
    )


def synthesize_pass(
    source,
    category,
    *,
    item_schema,
    aggregate_schema,
    top_n: int = 20,
    rank_by: str = "auto",     # v1: ranking comes from categorize (view_count, else order)
    focus: str = "",
    force: bool = False,
    model=GEMINI_MODEL,
    media_resolution_low=GEMINI_MEDIA_RESOLUTION_LOW,
) -> dict:
    """Fold the top-N of `category` into the persisted, compounding aggregate for
    (source, aggregate_schema). One video's extraction failure never aborts the pass.
    Returns:
      {status, category, added:[video_id], extraction_failed:[...], aggregate, aggregate_key,
       passes_so_far, truncated, skipped}
    """
    from screenscribe.gemini_selector import _call_gemini_text, gemini_available

    if not gemini_available():
        return {"status": "error", "error": "synthesize_pass needs GEMINI_API_KEY."}

    cats = categorize(source, force=force)
    if cats.get("status") != "success":
        return cats
    match = next((c for c in cats["categories"] if c["name"] == category), None)
    if match is None:
        return {"status": "error",
                "error": f"Unknown category '{category}'. Available: {[c['name'] for c in cats['categories']]}"}

    selected = match["video_ids"][:top_n]
    truncated = len(match["video_ids"]) > top_n

    agg_schema = resolve_aggregate_schema(aggregate_schema)
    agg_path = _aggregate_path(source, aggregate_schema)
    if agg_path.exists():
        state = json.loads(agg_path.read_text())
    else:
        state = {"aggregate": None, "included": [], "passes": 0}
    included = set(state["included"])

    to_extract = selected if force else [v for v in selected if v not in included]

    new_results, failed = [], []
    for video_id in to_extract:
        url = f"https://www.youtube.com/watch?v={video_id}"
        res = extract_structured(url, item_schema, focus=focus, force=force,
                                 model=model, media_resolution_low=media_resolution_low)
        if res.get("status") == "success":
            new_results.append({"video_id": video_id, "data": res["data"]})
        else:
            failed.append({"video_id": video_id, "status": res.get("status"), "error": res.get("error")})

    aggregate = state["aggregate"]
    if new_results:
        prompt = _build_aggregate_prompt(aggregate, new_results, category, agg_schema)
        raw = _call_gemini_text(model, prompt, agg_schema)
        ok, data, err = validate_output(raw, agg_schema)
        if not ok:
            raw = _call_gemini_text(
                model, prompt + f"\nYour previous output failed validation: {err}\nReturn corrected JSON.",
                agg_schema,
            )
            ok, data, err = validate_output(raw, agg_schema)
        if not ok:
            # Don't lose prior state on a bad aggregate call.
            return {"status": "invalid", "category": category, "error": err, "raw": raw,
                    "aggregate": aggregate, "extraction_failed": failed}
        aggregate = data

    state = {
        "aggregate": aggregate,
        "included": list(included | {r["video_id"] for r in new_results}),
        "passes": state["passes"] + 1,
        "source": source,
    }
    agg_path.parent.mkdir(parents=True, exist_ok=True)
    agg_path.write_text(json.dumps(state, indent=2))

    return {
        "status": "success",
        "category": category,
        "added": [r["video_id"] for r in new_results],
        "extraction_failed": failed,
        "aggregate": aggregate,
        "aggregate_key": f"{_source_key(source)}__{_aggregate_key(aggregate_schema)}",
        "passes_so_far": state["passes"],
        "truncated": truncated,
        "skipped": cats.get("ranked"),
    }
