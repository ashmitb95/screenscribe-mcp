# Schema-driven (typed) extraction — design

**Date:** 2026-06-10
**Status:** Approved, pre-implementation
**Roadmap item:** 3 (the keystone; precedes cross-video synthesis)

## Goal

Let a caller hand screenscribe a **shape** (a JSON Schema, or a named preset) and get back
**validated, typed JSON** extracted from a video — instead of prose. This turns screenscribe from a
reading aid into an automation primitive an agent can build on, and provides the substrate the next
feature (cross-video synthesis) extracts and aggregates over.

It generalizes the existing `analyze_video` path, which already asks Gemini for one fixed structured
shape (`summary`/`sections`/`key_moments`/`on_screen_text`) via a `response_schema`. Here the shape
is caller-supplied.

## Non-goals (v1 / YAGNI)

- No Claude-over-frames engine and no `--engine` selector. Gemini whole-video only.
- No per-step visual diffing beyond what a caller's schema asks Gemini to produce.
- No separate "get cached structured result" tool — re-calling the extractor with the same schema
  hits the cache and returns instantly (that is the retrieval path).

## Engine

**Gemini whole-video**, reusing `gemini_selector._call_gemini`. Verified: `google-genai` 2.7.0
exposes `GenerateContentConfig.response_json_schema` (accepts a raw JSON Schema) — so the caller's
JSON Schema passes straight through with no JSON-Schema→OpenAPI conversion.

Rationale: cheap (~$0.03), no download/ffmpeg, native temporal understanding, one code path, reuses
existing retry/SDK machinery. Trade-off: weaker on tiny on-screen detail (exact code characters) at
low media resolution — acceptable for v1; `media_resolution` is already tunable, and a
Claude-over-frames engine can be added later behind the same interface.

## Surface

### MCP tool (primary consumer = agents)

```
extract_structured(url: str, schema: dict | str, focus: str = "", time_range: str = "") -> str(JSON)
```
- `schema` may be a **preset name** (string, e.g. `"cli_commands"`) or a **full JSON Schema dict**.
- Returns validated JSON on success, or a structured error object (see Validation).

### CLI (mirror surface = humans / scripts)

```
screenscribe extract-structured <url> --schema <preset|path|inline-json> [--focus ...] [--time-range ...] [--force]
```
- Prints validated JSON to stdout (pipeable). Exits non-zero on unrecoverable validation failure.

## Schema resolution

`resolve_schema(arg)` resolution order:
1. **Preset name** — matches a bundled preset → load `schemas/<name>.json`.
2. **Existing file path** → load and parse it.
3. Otherwise **inline JSON** → parse the string directly.

The MCP `schema` param: a `dict` is used as-is (inline schema); a `str` goes through `resolve_schema`
(preset name or, less commonly, a path/JSON string).

## Presets (v1: six)

Bundled as `src/screenscribe/schemas/*.json`, shipped as package data. Each is a valid JSON Schema and
is tested against a real video before shipping. Timestamp fields are requested as **`seconds`
(number)** directly (not MM:SS) — cleaner for automation, no post-parse step.

| Preset | Shape | Extracts |
| --- | --- | --- |
| `cli_commands` | flat list | every command shown/run — `{command, context, seconds}` |
| `final_config` | single object | the end-state config/file — `{filename, language, content}` |
| `step_sequence` | temporal list | ordered steps/stages — `{step, seconds, action, detail}` |
| `code_blocks` | flat list | distinct code shown — `{language, code, explanation, seconds}` |
| `resources_mentioned` | flat list | external refs — `{name, type, url, context}` |
| `chapters` | temporal list | section breakdown — `{start_seconds, title, summary}` |

`chapters` overlaps `analyze_video`'s `sections` but is provided as a typed standalone shape.

## Validation & retry

`response_json_schema` makes Gemini *try* to match the shape but is best-effort (Gemini honors only a
subset of JSON Schema — nested `$ref`/`oneOf`/conditionals may not fully constrain). So output is
always validated against the caller's schema, which is the real contract.

Flow:
1. `_call_gemini(url, ..., response_json_schema=schema)` → raw JSON text.
2. `validate_output(raw_text, schema)` = `json.loads` + `jsonschema.validate`.
3. On parse/validation failure: **retry once**, feeding the validation error back into the prompt
   (*"Your previous output failed: `<error>`. Return corrected JSON matching the schema."*).
4. Still invalid after the retry: return a structured error — never malformed data as success:
   ```json
   {"status": "invalid", "error": "<validation message>", "raw": "<model output>"}
   ```
   CLI exits non-zero; the MCP tool returns the error object so the agent can react.

This mirrors the existing "never a silent cut" principle used for transcripts.

**Success shape.** The MCP tool returns:
```json
{"status": "success", "session_id": "<id>", "key": "<schema key>", "cached": false, "data": <validated JSON>}
```
The CLI prints **only `data`** (the typed JSON) to stdout, so it pipes cleanly into `jq`/another tool;
the wrapper metadata goes to stderr.

## Persistence (cache; feeds synthesis)

Cache per **(video_id, schema)**:
```
~/.video-analyzer/{video_id}/structured/{key}.json
```
- `key` = the preset name (`cli_commands`) for presets, or a short hash (e.g. first 12 hex of
  sha256) of the canonical schema JSON (sorted keys) for free-form schemas.
- Each cached file stores both the `schema` and the `result`, for transparency.
- Cache is checked before the Gemini call; `--force` / `force=True` bypasses and overwrites.

Why in v1: cross-video synthesis fans one schema over many videos; with the cache, re-running an
aggregation only re-pays for the aggregation, not each per-video extraction. ~15 lines, serves the
very next feature directly.

## Code structure

**New files:**
- `src/screenscribe/structured_extractor.py`
  - `load_preset(name) -> dict | None`
  - `resolve_schema(arg: str | dict) -> dict`
  - `build_extraction_prompt(schema: dict, focus: str) -> str`
  - `validate_output(raw_text: str, schema: dict) -> tuple[bool, dict | None, str]` — pure;
    returns (ok, data, error_message).
  - `extract_structured(url, model, schema, focus, time_range, media_resolution_low) -> dict` —
    network; orchestrates call → validate → retry → structured error/result.
  - cache helpers: `schema_key(schema_or_name)`, `cached_path(video_id, key)`, read/write.
- `src/screenscribe/schemas/*.json` — the six preset schemas.
- `tests/test_structured_extractor.py` — SDK-free unit tests (canned strings), matching existing
  test style.

**Touched files (small, additive):**
- `gemini_selector.py` — `_call_gemini` gains a `response_json_schema=None` passthrough param;
  passed into `GenerateContentConfig`. Existing `response_schema` behavior unchanged.
- `main.py` — new `extract-structured` subcommand + handler.
- `server.py` — new `extract_structured` MCP tool.
- `pyproject.toml` — add `jsonschema` as a direct dependency (currently transitive via `mcp[cli]`);
  confirm `schemas/*.json` ships as package data (hatchling includes package files by default).

**Reuses:** `_call_gemini` (video + retry + SDK), `gemini_available()` gate, `focus`/`time_range`
parsing, `session_dir` helpers.

## Testing

- `validate_output`: valid pass-through; schema violation → not-ok + message; malformed JSON →
  not-ok + message.
- `resolve_schema`: preset name → bundled schema; file path → file contents; inline JSON → parsed;
  unknown → clear error.
- Every shipped preset parses and is itself a valid JSON Schema (meta-validate).
- `schema_key`: stable across key reordering (canonicalization), preset names pass through.
- Retry-decision logic: given an invalid first output, the orchestrator issues exactly one retry.
  (Network mocked, as in `test_gemini_selector.py`.)

## Risks

- **Gemini JSON-Schema subset.** Complex schemas may not be fully constrained at generation time;
  validation catches mismatches, and the single retry recovers many. Documented limitation; keep
  preset schemas simple/flat. Free-form callers who use exotic constructs may hit the structured
  error path more often — acceptable and transparent.
- **Visual precision at low media resolution.** Exact on-screen code/config characters may be
  imperfect. Mitigation: presets that need precision can later route to a Claude-over-frames engine
  behind the same interface; out of scope for v1.
