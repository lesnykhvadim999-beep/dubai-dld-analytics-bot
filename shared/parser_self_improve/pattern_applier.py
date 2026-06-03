"""
pattern_applier — добавляет approved regex в parser_v2_extras.py + git commit.

Безопасный режим:
- НЕ модифицирует parser_v2.py напрямую.
- Дописывает в parser_v2_extras.py отдельную AUTO_PATTERNS registry с комментариями.
- Создаёт git commit, push НЕ делает (Vadim апрувит deploy отдельно).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime

from ._db import conn_cursor

log = logging.getLogger(__name__)

RESALE_DIR = os.environ.get("RESALE_BOT_DIR", r"C:\Projects\resale-bot")
EXTRAS_FILE = "parser_v2_extras.py"
AUTO_MARKER_START = "# >>> AUTO_PATTERNS (parser_self_improve) >>>"
AUTO_MARKER_END = "# <<< AUTO_PATTERNS (parser_self_improve) <<<"


def _read_extras() -> str:
    path = os.path.join(RESALE_DIR, EXTRAS_FILE)
    if not os.path.exists(path):
        # создадим минимальный stub
        return (
            '"""parser_v2_extras — место для auto-discovered patterns."""\n\n'
            "AUTO_PATTERNS: list[dict] = []\n\n"
            f"{AUTO_MARKER_START}\n"
            f"{AUTO_MARKER_END}\n"
        )
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _ensure_markers(content: str) -> str:
    if AUTO_MARKER_START in content and AUTO_MARKER_END in content:
        return content
    return content.rstrip() + (
        "\n\n# ───────────────────────────────────\n"
        f"{AUTO_MARKER_START}\n"
        f"{AUTO_MARKER_END}\n"
    )


def _insert_pattern(content: str, snippet: str) -> str:
    content = _ensure_markers(content)
    return content.replace(
        AUTO_MARKER_END,
        snippet.rstrip() + "\n" + AUTO_MARKER_END,
    )


def _build_snippet(cluster_id: int, rule: dict) -> str:
    regex = rule.get("regex", "")
    field = rule.get("field", "?")
    sym = rule.get("symptom", "")
    hint = rule.get("extractor_hint", "")
    ts = datetime.utcnow().isoformat()
    safe_regex = regex.replace("\\", "\\\\").replace('"', '\\"')
    return (
        f"\n# ── cluster #{cluster_id} ({ts}) ────────────────────────────\n"
        f"# Field: {field}\n"
        f"# Symptom: {sym}\n"
        f"# Hint: {hint}\n"
        "AUTO_PATTERNS.append({\n"
        f'    "cluster_id": {cluster_id},\n'
        f'    "field": "{field}",\n'
        f'    "regex": r"{safe_regex}",\n'
        f'    "added_at": "{ts}",\n'
        "})\n"
    )


def _git(*args: str) -> tuple[int, str, str]:
    p = subprocess.run(
        ["git", *args],
        cwd=RESALE_DIR,
        capture_output=True,
        text=True,
    )
    return p.returncode, p.stdout, p.stderr


def apply_approved_cluster(cluster_id: int) -> str | None:
    """Применить approved cluster: patch + commit. Возвращает SHA или None."""
    with conn_cursor(dict_cursor=True) as (_, cur):
        cur.execute(
            "SELECT * FROM parser_pattern_clusters WHERE id=%s AND status='approved'",
            (cluster_id,),
        )
        row = cur.fetchone()
    if not row:
        log.info("cluster %s not in 'approved' state", cluster_id)
        return None
    rule = row.get("suggested_rule_json") or {}
    if not isinstance(rule, dict):
        log.warning("cluster %s: suggested_rule_json is not dict", cluster_id)
        return None
    # back-compat: v1 использовал 'regex', v2 — 'pattern'. Нормализуем в обе стороны.
    pat = rule.get("pattern") or rule.get("regex")
    if not pat:
        log.warning("cluster %s missing rule.pattern/regex", cluster_id)
        return None
    rule.setdefault("regex", pat)
    rule.setdefault("pattern", pat)

    path = os.path.join(RESALE_DIR, EXTRAS_FILE)
    content = _read_extras()
    snippet = _build_snippet(cluster_id, rule)
    new_content = _insert_pattern(content, snippet)
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_content)

    # Sanity check: file должен импортироваться
    rc, _, err = subprocess.run(
        ["py", "-c", f"import ast; ast.parse(open(r'{path}', encoding='utf-8').read())"],
        capture_output=True,
        text=True,
    ).returncode, "", ""
    # (subprocess.run возвращает CompletedProcess; упростим:)
    cp = subprocess.run(
        ["py", "-c", f"import ast; ast.parse(open(r'{path}', encoding='utf-8').read())"],
        capture_output=True,
        text=True,
    )
    if cp.returncode != 0:
        log.warning("AST parse failed for extras: %s — reverting", cp.stderr)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return None

    # git commit
    rc, out, err = _git("add", EXTRAS_FILE)
    if rc != 0:
        log.warning("git add failed: %s", err)
        return None
    msg = f"feat(parser): auto-add pattern from cluster #{cluster_id}"
    rc, out, err = _git("commit", "-m", msg)
    if rc != 0:
        log.warning("git commit failed: %s / %s", out, err)
        return None
    rc, sha, _ = _git("rev-parse", "HEAD")
    sha = sha.strip() if rc == 0 else None
    if sha:
        with conn_cursor() as (_, cur):
            cur.execute(
                "UPDATE parser_pattern_clusters SET status='applied', applied_at=NOW(), applied_commit_sha=%s WHERE id=%s",
                (sha[:40], cluster_id),
            )
    return sha
