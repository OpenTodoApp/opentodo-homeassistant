"""The translation files stay in step with strings.json."""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

BASE = Path(__file__).parent.parent / "custom_components" / "opentodo"
STRINGS = json.loads((BASE / "strings.json").read_text())
LANGUAGES = ["en", "it", "de", "fr", "es", "pt"]


def flat(tree: dict, prefix: str = "") -> dict[str, str]:
    """Nested keys as dotted paths."""
    out: dict[str, str] = {}
    for key, value in tree.items():
        path = f"{prefix}.{key}" if prefix else key
        out.update(flat(value, path) if isinstance(value, dict) else {path: value})
    return out


def test_english_is_strings() -> None:
    """translations/en.json is strings.json (custom integrations load the translations folder)."""
    assert json.loads((BASE / "translations" / "en.json").read_text()) == STRINGS


@pytest.mark.parametrize("language", LANGUAGES)
def test_same_keys_and_placeholders(language: str) -> None:
    """Every language has every string, with the same placeholders."""
    source = flat(STRINGS)
    translated = flat(json.loads((BASE / "translations" / f"{language}.json").read_text()))
    assert translated.keys() == source.keys()
    for key, text in source.items():
        assert sorted(re.findall(r"\{\w+\}", translated[key])) == sorted(re.findall(r"\{\w+\}", text)), key
