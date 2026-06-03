"""chart_annotations — overlay trend / anomaly / median markers on matplotlib axes.

Used by both the existing dynamics chart in vadim_pdf and the new forecast chart
in price_forecast.py. Standalone helper — operates on an Axes object that the
caller already configured.
"""
from __future__ import annotations

import logging
from typing import List, Optional, Sequence

log = logging.getLogger("pdf_intelligence.annotations")

GOLD = "#C9A84C"
RED_ALERT = "#FF4444"
GREEN_GROW = "#16A34A"
GRAY_BASE = "#888888"
RED_BASE = "#E05252"


def annotate_trend_chart(ax,
                         values: Sequence[float],
                         area_median: Optional[float] = None,
                         dubai_median: Optional[float] = None,
                         anomaly_threshold: float = 0.10,
                         show_yoy: bool = True) -> None:
    """Decorate an Axes object with trend / anomaly / median annotations.

    Args:
        ax: matplotlib Axes (already plotted with values).
        values: Sequence of values (oldest to newest) matching the x-axis indexes.
        area_median: If set, draw red dashed horizontal line.
        dubai_median: If set, draw gray dashed horizontal line.
        anomaly_threshold: Fraction (0.10 = 10%) defining what counts as anomaly.
        show_yoy: Add `↗ +X% YoY` annotation near the last point.
    """
    try:
        if not values or len(values) < 2:
            return

        vs: List[float] = [float(v) for v in values if v is not None]
        if len(vs) < 2:
            return

        # ── median baselines ─────────────────────────────────────────────
        if area_median and area_median > 0:
            try:
                ax.axhline(area_median, color=RED_BASE, linestyle="--",
                           linewidth=0.9, alpha=0.55,
                           label=f"Area median {area_median:,.0f}")
            except Exception:
                pass
        if dubai_median and dubai_median > 0:
            try:
                ax.axhline(dubai_median, color=GRAY_BASE, linestyle="--",
                           linewidth=0.9, alpha=0.45,
                           label=f"Dubai median {dubai_median:,.0f}")
            except Exception:
                pass

        # ── YoY trend annotation (last point) ────────────────────────────
        if show_yoy and len(vs) >= 2:
            base_idx = max(0, len(vs) - 12)
            base_val = vs[base_idx]
            last_val = vs[-1]
            if base_val and base_val > 0:
                yoy = (last_val - base_val) / base_val * 100
                arrow = "↗" if yoy >= 0 else "↘"
                color = GREEN_GROW if yoy >= 0 else RED_ALERT
                try:
                    ax.annotate(
                        f"{arrow} {yoy:+.1f}% YoY",
                        xy=(len(vs) - 1, last_val),
                        xytext=(max(0, len(vs) - 4), last_val * 1.06),
                        color=color, fontsize=9, fontweight="bold",
                        arrowprops=dict(arrowstyle="-", color=color, alpha=0.4, lw=0.7),
                    )
                except Exception:
                    pass

        # ── anomaly markers (>= threshold MoM jumps) ────────────────────
        for i in range(1, len(vs)):
            prev = vs[i - 1]
            if not prev or prev <= 0:
                continue
            change = (vs[i] - prev) / prev
            if abs(change) < anomaly_threshold:
                continue
            up = change > 0
            color = GREEN_GROW if up else RED_ALERT
            label = ("+%.0f%%" % (change * 100)) if up else ("%.0f%%" % (change * 100))
            try:
                ax.scatter([i], [vs[i]], color=color, s=80, zorder=6,
                           edgecolors="white", linewidths=1.2)
                ax.annotate(label,
                            xy=(i, vs[i]),
                            xytext=(i + 0.3, vs[i] * (1.05 if up else 0.92)),
                            color=color, fontsize=8, fontweight="bold")
            except Exception:
                pass

        # ── legend (only if we added any) ────────────────────────────────
        if area_median or dubai_median:
            try:
                ax.legend(loc="lower right", fontsize=7, frameon=False)
            except Exception:
                pass
    except Exception as e:
        log.debug(f"annotate_trend_chart failed: {e}")


if __name__ == "__main__":
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(6, 3))
    vs = [20000, 20300, 20500, 21000, 19500, 21800, 22200, 22500,
          22800, 23000, 23300, 23600]
    ax.plot(vs, color=GOLD, linewidth=2)
    annotate_trend_chart(ax, vs, area_median=22000, dubai_median=20500)
    fig.tight_layout()
    fig.savefig("_test_annotations.png", dpi=110)
    print("OK -> _test_annotations.png")
