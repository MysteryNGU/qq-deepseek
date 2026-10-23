"""Offline acceptance for the current runtime; no model or QQ requests."""
from pathlib import Path
import unittest


def run_checks(services_only=False):
    root = Path(__file__).resolve().parents[1]
    pattern = 'test_dsh_*.py' if services_only else 'test_*.py'
    suite = unittest.defaultTestLoader.discover(str(root / 'tests'), pattern=pattern, top_level_dir=str(root))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1
