"""Guard against config-example.py drifting behind required cfg.<UPPER> keys.

Regression for staging finding F1 (2026-10-31): layout_strategy.py reads
cfg.COVERAGE_VETO_RATIO directly, but config-example.py did not define it, so
a fresh installation crashed in daily rendering with AttributeError while the
operator's own config.py kept working.
"""
import re
import runpy
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config-example.py"
_EXCLUDED_COMPONENTS = {".venv", "archive", "tests", "__pycache__", ".zcode"}


def _referenced_config_keys() -> set[str]:
    refs: set[str] = set()
    patterns = (re.compile(r"\bcfg\.([A-Z][A-Z0-9_]+)\b"),
                re.compile(r"\bconfig\.([A-Z][A-Z0-9_]+)\b"))
    for source in ROOT.rglob("*.py"):
        if _EXCLUDED_COMPONENTS.intersection(source.parts):
            continue
        text = source.read_text(encoding="utf-8", errors="ignore")
        for pattern in patterns:
            refs.update(pattern.findall(text))
    return refs


def _example_namespace() -> dict:
    return runpy.run_path(str(EXAMPLE))


def _example_defined_keys() -> set[str]:
    namespace = _example_namespace()
    return {key for key in namespace if key.isupper() and not key.startswith("_")}


class ConfigExampleCompletenessTests(unittest.TestCase):
    def test_example_defines_every_hard_referenced_key(self):
        missing = _referenced_config_keys() - _example_defined_keys()
        self.assertEqual(
            sorted(missing), [],
            "config-example.py is missing keys that code reads as cfg.<UPPER>; "
            "add them to config-example.py or switch the code to getattr defaults")

    def test_regression_coverage_veto_ratio(self):
        self.assertIn("COVERAGE_VETO_RATIO", _example_defined_keys())

    def test_coverage_veto_ratio_is_numeric(self):
        value = _example_namespace()["COVERAGE_VETO_RATIO"]
        self.assertIsInstance(value, (int, float))


if __name__ == "__main__":
    unittest.main()
