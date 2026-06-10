import json
from pathlib import Path

import jsonschema
import pytest

SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "src" / "screenscribe" / "schemas"

EXPECTED_PRESETS = {
    "cli_commands", "final_config", "step_sequence",
    "code_blocks", "resources_mentioned", "chapters", "recipe",
}


def test_all_expected_presets_exist():
    found = {p.stem for p in SCHEMAS_DIR.glob("*.json")}
    assert EXPECTED_PRESETS <= found, f"missing presets: {EXPECTED_PRESETS - found}"


@pytest.mark.parametrize("name", sorted(EXPECTED_PRESETS))
def test_preset_is_valid_json_schema(name):
    schema = json.loads((SCHEMAS_DIR / f"{name}.json").read_text())
    # Raises SchemaError if the schema itself is malformed.
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema.get("type") == "object"
