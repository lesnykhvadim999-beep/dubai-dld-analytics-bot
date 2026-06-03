"""Daily audit section formatter — plug into shared.auto_audit.daily_audit."""
from __future__ import annotations

from .client import KG


def render_section() -> str:
    s = KG.stats()
    total = s.get("total", 0)
    appr = s.get("approved", 0)
    pend = s.get("pending", 0)
    rej = s.get("rejected", 0)
    used = s.get("used_24h", 0)
    vrate = s.get("validation_rate", 0)
    top = s.get("by_category", []) or []
    top_str = ", ".join(f"{r['category']} ({r['n']})" for r in top[:3]) or "—"
    lines = [
        "🧠 KNOWLEDGE GRAPH",
        f"├ Total entries: {total} ({appr} approved, {pend} pending, {rej} rejected)",
        f"├ Used last 24h: {used:,} times",
        f"├ Top categories: {top_str}",
        f"├ Validation rate: {vrate}%",
        f"└ ⏳ {pend} pending Vadim approval" if pend else "└ ✅ no pending entries",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    print(render_section())
