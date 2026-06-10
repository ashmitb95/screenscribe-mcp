"""
Tests for cross-video synthesis. The resolver, the text Gemini call, and per-video
extraction are all monkeypatched, so these run with no network and no API key —
mirroring the SDK-free style in test_structured_extractor.py.
"""

import json

import pytest

import screenscribe.gemini_selector as gs
import screenscribe.resolver as rv
import screenscribe.session as sess
import screenscribe.synthesis as syn


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(sess, "SESSIONS_DIR", tmp_path)          # synthesis writes under here
    monkeypatch.setattr(gs, "gemini_available", lambda: True)


def _resolver(monkeypatch, videos, kind="channel"):
    monkeypatch.setattr(rv, "resolve_videos", lambda source, **k: {
        "kind": kind, "video_ids": [v["id"] for v in videos], "videos": videos,
        "title": "chan", "skipped": {"too_short": 0, "unavailable": 0},
        "total_found": len(videos),
    })


# ── categorize ───────────────────────────────────────────────────────────────

def test_categorize_classifies_buckets_and_ranks_by_views(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _resolver(monkeypatch, [
        {"id": "v1", "title": "Veg Curry", "duration": 600, "view_count": 100},
        {"id": "v2", "title": "Fish Fry", "duration": 600, "view_count": 900},
        {"id": "v3", "title": "Aloo Veg", "duration": 600, "view_count": 500},
    ])
    monkeypatch.setattr(gs, "_call_gemini_text", lambda *a, **k: json.dumps({
        "categories": ["veg", "fish"],
        "assignments": [
            {"video_id": "v1", "category": "veg"},
            {"video_id": "v2", "category": "fish"},
            {"video_id": "v3", "category": "veg"},
        ],
    }))
    out = syn.categorize("https://youtube.com/@chan")
    assert out["status"] == "success"
    assert out["ranked"] is True
    veg = next(c for c in out["categories"] if c["name"] == "veg")
    assert veg["video_ids"] == ["v3", "v1"]          # ranked by view_count desc (500 > 100)
    assert out["by_id"]["v2"] == "fish"


def test_categorize_unassigned_become_uncategorized(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _resolver(monkeypatch, [
        {"id": "v1", "title": "A", "duration": 1, "view_count": None},
        {"id": "v2", "title": "B", "duration": 1, "view_count": None},
    ])
    monkeypatch.setattr(gs, "_call_gemini_text", lambda *a, **k: json.dumps({
        "categories": ["x"], "assignments": [{"video_id": "v1", "category": "x"}],  # v2 omitted
    }))
    out = syn.categorize("https://youtube.com/@chan")
    assert out["by_id"]["v2"] == "uncategorized"     # never silently dropped
    assert out["ranked"] is False


def test_categorize_is_cached(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _resolver(monkeypatch, [{"id": "v1", "title": "A", "duration": 1, "view_count": 1}])
    calls = []
    monkeypatch.setattr(gs, "_call_gemini_text", lambda *a, **k: (
        calls.append(1), json.dumps({"categories": ["x"], "assignments": [{"video_id": "v1", "category": "x"}]}))[1])
    syn.categorize("https://youtube.com/@chan")
    syn.categorize("https://youtube.com/@chan")
    assert len(calls) == 1                            # second served from cache


def test_categorize_requires_gemini(monkeypatch, tmp_path):
    monkeypatch.setattr(sess, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(gs, "gemini_available", lambda: False)
    _resolver(monkeypatch, [{"id": "v1", "title": "A", "duration": 1, "view_count": 1}])
    out = syn.categorize("https://youtube.com/@chan")
    assert out["status"] == "error"


# ── synthesize_pass (compounding) ─────────────────────────────────────────────

def _mock_categories(monkeypatch, video_ids):
    monkeypatch.setattr(syn, "categorize", lambda source, **k: {
        "status": "success", "source": source, "source_key": "k", "kind": "channel",
        "categories": [{"name": "veg", "count": len(video_ids), "video_ids": video_ids}],
        "by_id": {}, "ranked": True, "total": len(video_ids),
    })


def test_synthesize_pass_extracts_topn_and_aggregates(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _mock_categories(monkeypatch, ["v1", "v2", "v3"])
    extracted = []

    def fake_extract(url, schema, **k):
        vid = url.split("v=")[-1]
        extracted.append(vid)
        return {"status": "success", "data": {"dish": vid}, "cached": False}

    monkeypatch.setattr(syn, "extract_structured", fake_extract)
    monkeypatch.setattr(gs, "_call_gemini_text", lambda model, prompt, schema: json.dumps(
        {"categories": [{"name": "veg", "recipes": [{"dish": "x"}]}]}))

    out = syn.synthesize_pass("src", "veg", item_schema="recipe",
                              aggregate_schema="cookbook", top_n=2)
    assert out["status"] == "success"
    assert extracted == ["v1", "v2"]                 # capped at top_n=2
    assert out["added"] == ["v1", "v2"]
    assert out["truncated"] is True                  # 3 available, 2 taken
    assert out["passes_so_far"] == 1
    assert out["aggregate"]["categories"][0]["name"] == "veg"


def test_synthesize_pass_compounds_and_skips_included(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _mock_categories(monkeypatch, ["v1", "v2"])
    monkeypatch.setattr(syn, "extract_structured",
                        lambda url, schema, **k: {"status": "success", "data": {"dish": url[-2:]}, "cached": False})

    seen_priors = []

    def fake_text(model, prompt, schema):
        seen_priors.append(prompt)
        return json.dumps({"categories": [{"name": "veg", "recipes": [{"dish": "x"}]}]})

    monkeypatch.setattr(gs, "_call_gemini_text", fake_text)

    p1 = syn.synthesize_pass("src", "veg", item_schema="recipe", aggregate_schema="cookbook", top_n=1)
    assert p1["added"] == ["v1"]
    p2 = syn.synthesize_pass("src", "veg", item_schema="recipe", aggregate_schema="cookbook", top_n=2)
    # v1 already folded in → only v2 extracted this pass
    assert p2["added"] == ["v2"]
    assert p2["passes_so_far"] == 2
    # pass 2's aggregate prompt carries the existing (non-null) aggregate from pass 1
    assert "EXISTING AGGREGATE" in seen_priors[-1]
    assert '"categories"' in seen_priors[-1].split("NEW PER-VIDEO RESULTS")[0]


def test_synthesize_pass_isolates_extraction_failure(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _mock_categories(monkeypatch, ["v1", "v2"])

    def fake_extract(url, schema, **k):
        if url.endswith("v2"):
            return {"status": "invalid", "error": "bad", "raw": "{}"}
        return {"status": "success", "data": {"dish": "ok"}, "cached": False}

    monkeypatch.setattr(syn, "extract_structured", fake_extract)
    monkeypatch.setattr(gs, "_call_gemini_text", lambda *a, **k: json.dumps(
        {"categories": [{"name": "veg", "recipes": [{"dish": "ok"}]}]}))

    out = syn.synthesize_pass("src", "veg", item_schema="recipe", aggregate_schema="cookbook", top_n=2)
    assert out["added"] == ["v1"]
    assert [f["video_id"] for f in out["extraction_failed"]] == ["v2"]
    assert out["status"] == "success"                # one failure didn't abort the pass


def test_synthesize_pass_unknown_category(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _mock_categories(monkeypatch, ["v1"])
    out = syn.synthesize_pass("src", "dessert", item_schema="recipe", aggregate_schema="cookbook")
    assert out["status"] == "error"
    assert "dessert" in out["error"]


def test_synthesize_pass_whole_set_no_category(monkeypatch, tmp_path):
    # category=None → synthesize over the whole resolved set (no categorize call).
    _setup(monkeypatch, tmp_path)
    _resolver(monkeypatch, [
        {"id": "a", "title": "t", "duration": 1, "view_count": None},
        {"id": "b", "title": "t", "duration": 1, "view_count": None},
    ], kind="list")

    def boom(*a, **k):
        raise AssertionError("categorize must not be called when category is None")
    monkeypatch.setattr(syn, "categorize", boom)
    monkeypatch.setattr(syn, "extract_structured",
                        lambda url, schema, **k: {"status": "success", "data": {"dish": url[-1]}, "cached": False})
    monkeypatch.setattr(gs, "_call_gemini_text", lambda *a, **k: json.dumps(
        {"categories": [{"name": "all", "recipes": [{"dish": "x"}]}]}))

    out = syn.synthesize_pass(["u1", "u2"], None, item_schema="recipe",
                              aggregate_schema="cookbook", top_n=5)
    assert out["status"] == "success"
    assert out["category"] == "all"
    assert set(out["added"]) == {"a", "b"}


# ── CLI + MCP surface ─────────────────────────────────────────────────────────

def test_mcp_synthesis_tools_registered():
    from screenscribe.server import mcp
    tools = set(mcp._tool_manager._tools)
    assert {"synthesize_categorize", "synthesize_pass"} <= tools
    assert len(tools) == 9


def test_cli_synthesize_pass_plumbing(monkeypatch, capsys):
    import types
    import screenscribe.main as m
    monkeypatch.setattr(syn, "synthesize_pass", lambda *a, **k: {
        "status": "success", "category": "all", "added": ["v1"], "extraction_failed": [],
        "passes_so_far": 1, "truncated": False, "aggregate": {"x": 1}, "aggregate_key": "k",
    })
    args = types.SimpleNamespace(source="src", category="", item_schema="recipe",
                                 aggregate_schema="cookbook", top_n=5, focus="",
                                 force=False, media_res="low")
    m.cmd_synthesize_pass(args)
    cap = capsys.readouterr()
    assert json.loads(cap.out) == {"x": 1}     # aggregate → stdout (pipeable)
    assert "added=1" in cap.err                # summary → stderr
