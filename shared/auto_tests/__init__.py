"""Self-replicating regression tests (Level 8 of PHASE BL).

When a bugfix commit lands, generator.py produces a pytest case under
shared/auto_tests/tests/ that exercises the bug scenario. Pre-commit
runner executes these tests; failures block the commit.

Tests must be idempotent and DB-isolated (no prod hits).
"""
