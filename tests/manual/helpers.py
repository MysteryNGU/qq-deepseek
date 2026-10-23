"""Small helpers shared by explicitly invoked integration checks."""
import json
from pathlib import Path


def toml_value(value):
    if isinstance(value, dict):
        return '{ ' + ', '.join(json.dumps(str(k), ensure_ascii=False) + ' = ' + toml_value(v) for k, v in value.items()) + ' }'
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def report_path(name):
    directory = Path(__file__).resolve().parents[2] / 'data' / 'test-results'
    directory.mkdir(parents=True, exist_ok=True)
    return directory / name
