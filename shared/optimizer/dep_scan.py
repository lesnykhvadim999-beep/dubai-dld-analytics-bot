"""PHASE BN N5 — dependency vulnerability scanner (pip-audit).

For each `requirements.txt` under C:/Projects/*/requirements.txt run
`pip-audit --requirement <path> --format json` and store findings in
`vulnerability_findings`.  CRITICAL findings trigger an admin alert.

Usage:
    python -m shared.optimizer.dep_scan
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path

from . import _db

log = logging.getLogger("optimizer.dep_scan")
if not log.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
    )

PYTHON = sys.executable or "python"
PROJECTS_ROOT = Path(r"C:\Projects")

_SEVERITY_RANK = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 0}


def _ensure_pip_audit() -> bool:
    if shutil.which("pip-audit"):
        return True
    try:
        subprocess.run([PYTHON, "-c", "import pip_audit"],
                       check=True, capture_output=True)
        return True
    except Exception:
        pass
    log.info("pip-audit not installed — pip install pip-audit")
    try:
        subprocess.run([PYTHON, "-m", "pip", "install", "--quiet", "pip-audit"],
                       check=True, capture_output=True, timeout=240)
        return True
    except Exception as e:
        log.warning("pip install pip-audit failed: %s", e)
        return False


def _find_requirements() -> list[Path]:
    pattern = str(PROJECTS_ROOT / "*" / "requirements.txt")
    paths = [Path(p) for p in glob.glob(pattern)]
    # also pick up nested ones one level down (e.g. ROI-bot/requirements.txt)
    pattern2 = str(PROJECTS_ROOT / "*" / "*" / "requirements.txt")
    for p in glob.glob(pattern2):
        paths.append(Path(p))
    # de-dupe and skip vendored/.venv copies
    out: list[Path] = []
    seen: set[str] = set()
    for p in paths:
        s = str(p)
        low = s.lower()
        if any(part in low for part in (".venv", "site-packages",
                                        "node_modules", "__pycache__")):
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(p)
    return out


def _run_audit(req: Path) -> list[dict]:
    cmd = [PYTHON, "-m", "pip_audit",
           "--requirement", str(req),
           "--format", "json",
           "--progress-spinner", "off",
           "--disable-pip"]
    log.info("audit %s", req)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        log.warning("pip-audit timed out for %s", req)
        return []
    out = r.stdout or ""
    if not out.strip():
        return []
    try:
        data = json.loads(out)
    except Exception as e:
        log.warning("pip-audit JSON parse failed for %s: %s", req, e)
        return []
    # pip-audit JSON shape: {"dependencies":[{"name","version","vulns":[{"id","fix_versions","description"}, ...]}, ...]}
    findings: list[dict] = []
    for dep in data.get("dependencies", []):
        name = dep.get("name") or ""
        ver = dep.get("version") or ""
        for v in dep.get("vulns", []) or []:
            cve = v.get("id") or ""
            fixes = v.get("fix_versions") or []
            sev = (v.get("severity")
                   or _infer_severity(v.get("description") or "")
                   or "UNKNOWN").upper()
            findings.append({
                "package": name,
                "current_version": ver,
                "cve": cve,
                "severity": sev,
                "fix_version": ",".join(fixes)[:200] if fixes else None,
                "requirements_file": str(req),
            })
    return findings


def _infer_severity(desc: str) -> str:
    d = (desc or "").lower()
    if "critical" in d:
        return "CRITICAL"
    if "high" in d:
        return "HIGH"
    if "medium" in d or "moderate" in d:
        return "MEDIUM"
    if "low" in d:
        return "LOW"
    return "UNKNOWN"


def _persist(findings: list[dict]) -> int:
    conn = _db.connect()
    if conn is None:
        log.warning("no DSN — not persisting %d findings", len(findings))
        return 0
    inserted = 0
    try:
        with conn.cursor() as cur:
            for f in findings:
                try:
                    cur.execute(
                        """INSERT INTO vulnerability_findings
                           (package, current_version, cve, severity, fix_version)
                           VALUES (%s,%s,%s,%s,%s)""",
                        (f["package"][:200], f["current_version"][:80],
                         f["cve"][:80], f["severity"][:20],
                         f["fix_version"]),
                    )
                    inserted += 1
                except Exception as e:
                    log.warning("insert fail %s: %s", f.get("cve"), e)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return inserted


def _alert_critical(findings: list[dict]) -> None:
    crit = [f for f in findings if f.get("severity") == "CRITICAL"]
    if not crit:
        return
    try:
        from shared.admin_notify import admin_notify  # type: ignore
    except Exception:
        try:
            from admin_notify import admin_notify  # type: ignore
        except Exception:
            log.warning("admin_notify unavailable — %d CRITICAL vulns not alerted",
                        len(crit))
            return
    lines = [f"PHASE BN N5 — CRITICAL vulnerabilities: {len(crit)}"]
    for f in crit[:10]:
        lines.append(f"- {f['package']} {f['current_version']} "
                     f"{f['cve']} fix={f['fix_version'] or '?'}")
    try:
        admin_notify("\n".join(lines))
    except Exception as e:
        log.warning("admin_notify failed: %s", e)


def scan() -> list[dict]:
    if not _ensure_pip_audit():
        return []
    reqs = _find_requirements()
    log.info("found %d requirements.txt files", len(reqs))
    all_findings: list[dict] = []
    for r in reqs:
        all_findings.extend(_run_audit(r))
    log.info("total findings: %d", len(all_findings))
    inserted = _persist(all_findings)
    log.info("persisted %d", inserted)
    _alert_critical(all_findings)
    return all_findings


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser().parse_args(argv)  # accept no flags, for parity
    findings = scan()
    sev_counts: dict[str, int] = {}
    for f in findings:
        sev_counts[f["severity"]] = sev_counts.get(f["severity"], 0) + 1
    print(f"[optimizer.dep_scan] findings={len(findings)} {sev_counts}")
    for f in findings[:10]:
        print(f"  [{f['severity']}] {f['package']} {f['current_version']} "
              f"{f['cve']} fix={f['fix_version'] or '?'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
