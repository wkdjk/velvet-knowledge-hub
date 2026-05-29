"""
validate_config.py — Velvet Knowledge Hub config validator.

Reads config.yaml from the repo root and validates:
  1. Every source's `kind` is in the `display_kinds` list.
  2. Every source has all required fields: id, tab, kind, section, enabled.

Exits with code 1 and a clear error message on any failure.
Exits with code 0 and a success summary if all sources pass.

Usage (from repo root):
    PYTHONPATH=. python scripts/validate_config.py

Designed to run as a CI gate in GitHub Actions (L-1: PYTHONPATH=. required).
No external dependencies beyond PyYAML.
"""

import sys
from pathlib import Path

import yaml

# Required fields that every source block must contain.
REQUIRED_SOURCE_FIELDS = {"id", "tab", "kind", "section", "enabled"}

# Path to config relative to this script's parent (repo root).
CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


def load_config(path: Path) -> dict:
    """Load and return the YAML config. Exits on file-not-found or parse error."""
    if not path.exists():
        print(f"ERROR: config.yaml not found at {path}", file=sys.stderr)
        sys.exit(1)
    with path.open("r", encoding="utf-8") as fh:
        try:
            return yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            print(f"ERROR: Failed to parse config.yaml — {exc}", file=sys.stderr)
            sys.exit(1)


def validate(config: dict) -> list[str]:
    """
    Run all validation rules against the config dict.
    Returns a list of error strings. Empty list means all clear.
    """
    errors: list[str] = []

    # --- Top-level required keys -------------------------------------------
    for key in ("display_kinds", "sources"):
        if key not in config:
            errors.append(f"Missing top-level key: '{key}'")

    if errors:
        # Cannot proceed without these keys.
        return errors

    display_kinds: list = config["display_kinds"]
    sources: list = config["sources"]

    if not isinstance(display_kinds, list) or len(display_kinds) == 0:
        errors.append("'display_kinds' must be a non-empty list")
        return errors

    if not isinstance(sources, list):
        errors.append("'sources' must be a list")
        return errors

    # --- Per-source validation ---------------------------------------------
    for i, source in enumerate(sources):
        source_label = f"sources[{i}]"
        if isinstance(source.get("id"), str):
            source_label = f"source '{source['id']}'"

        # Check required fields are present and non-empty.
        for field in REQUIRED_SOURCE_FIELDS:
            if field not in source:
                errors.append(f"{source_label}: missing required field '{field}'")
            elif source[field] is None or source[field] == "":
                errors.append(
                    f"{source_label}: required field '{field}' must not be empty"
                )

        # Check kind is in display_kinds.
        kind = source.get("kind")
        if kind is not None and kind not in display_kinds:
            errors.append(
                f"{source_label}: kind '{kind}' is not in display_kinds "
                f"({', '.join(display_kinds)})"
            )

    return errors


def main() -> None:
    config = load_config(CONFIG_PATH)
    errors = validate(config)

    if errors:
        print("VALIDATION FAILED — config.yaml has errors:\n", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        print(
            f"\n{len(errors)} error(s) found. Fix config.yaml and re-run.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Success summary.
    sources = config["sources"]
    enabled = [s for s in sources if s.get("enabled")]
    disabled = [s for s in sources if not s.get("enabled")]

    print("validate_config.py — all checks passed")
    print(f"  display_kinds : {len(config['display_kinds'])} kinds defined")
    print(f"  sources total : {len(sources)}")
    print(f"  enabled       : {len(enabled)}")
    print(f"  disabled      : {len(disabled)}")
    if disabled:
        print(f"  disabled ids  : {[s['id'] for s in disabled]}")
    print("  result        : OK")


if __name__ == "__main__":
    main()
