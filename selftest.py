"""Offline runtime acceptance; no QQ or model requests."""
from life.checks import run_checks

if __name__ == "__main__":
    raise SystemExit(run_checks())
