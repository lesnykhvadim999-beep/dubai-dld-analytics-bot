"""Template skeleton used by generator.py when LLM is unavailable.

A bug regression test should:
  1. Mock DB / external services (no prod hits).
  2. Reproduce the failing scenario from the bug description.
  3. Assert the symptom does NOT recur.

Naming: tests/test_b{NNN}_<slug>.py
"""
import pytest


def test_placeholder_template():
    """Template test — replaced by generator.py."""
    assert True, "Template auto-test; replace with real regression."
