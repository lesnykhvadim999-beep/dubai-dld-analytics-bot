"""price_forecast — 12 / 24 / 36 month price forecast (LinReg + CI).

Pure-numpy implementation (no sklearn required — sklearn is optional speedup).
Returns point estimates and ±CI bands; flags low confidence when historical
train/test RMSE > 30% of mean price.
"""
from __future__ import annotations

import io
import logging
from typing import List, Optional, Tuple

log = logging.getLogger("pdf_intelligence.forecast")

try:
    import numpy as np
    NP_OK = True
except Exception:
    NP_OK = False
    np = None  # type: ignore

# sklearn is optional — if absent we use numpy.polyfit
try:
    from sklearn.linear_model import LinearRegression  # type: ignore
    SKLEARN_OK = True
except Exception:
    SKLEARN_OK = False

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    MPL_OK = True
except Exception:
    MPL_OK = False


# ── core forecast ─────────────────────────────────────────────────────────
def _fit_linear(y: "np.ndarray") -> Tuple[float, float]:
    """Return (slope, intercept) of best-fit line through y."""
    n = len(y)
    if SKLEARN_OK:
        X = np.arange(n).reshape(-1, 1)
        m = LinearRegression().fit(X, y)
        return float(m.coef_[0]), float(m.intercept_)
    # numpy polyfit fallback
    slope, intercept = np.polyfit(np.arange(n), y, 1)
    return float(slope), float(intercept)


def forecast_prices(historical: List[float],
                    months_ahead: int = 36) -> dict:
    """Forecast monthly median prices for the next `months_ahead` months.

    Args:
        historical: list of monthly median prices (oldest → newest).
        months_ahead: forecast horizon in months.

    Returns:
        dict with keys:
            point:       list[float] — point estimates
            lower:       list[float] — lower CI bound (~85% widening with horizon)
            upper:       list[float] — upper CI bound
            confidence:  'high' | 'medium' | 'low'
            rmse_pct:    float — train/test RMSE as % of mean price
            historical:  list[float] — passed-through input
    """
    if not NP_OK or len(historical) < 3:
        # degenerate input — flat-line "forecast" at last value
        last = float(historical[-1]) if historical else 0.0
        return {
            "point": [last] * months_ahead,
            "lower": [last * 0.85] * months_ahead,
            "upper": [last * 1.15] * months_ahead,
            "confidence": "low",
            "rmse_pct": 100.0,
            "historical": list(historical),
        }

    y = np.array([float(v) for v in historical if v is not None], dtype=float)
    n = len(y)

    # ── confidence calibration: 60% train / 40% test backtest ──────────
    rmse_pct = 0.0
    if n >= 6:
        split = max(3, int(n * 0.6))
        slope, icpt = _fit_linear(y[:split])
        pred_test = slope * np.arange(split, n) + icpt
        actual_test = y[split:]
        if len(actual_test) > 0 and y.mean() > 0:
            rmse = float(np.sqrt(np.mean((pred_test - actual_test) ** 2)))
            rmse_pct = rmse / y.mean() * 100.0

    if rmse_pct > 30:
        confidence = "low"
    elif rmse_pct > 15:
        confidence = "medium"
    else:
        confidence = "high"

    # ── forecast on full history ───────────────────────────────────────
    slope, icpt = _fit_linear(y)
    future_x = np.arange(n, n + months_ahead)
    point = slope * future_x + icpt
    # clamp non-negative
    point = np.maximum(point, y.min() * 0.5)

    # CI widens with horizon (±15% baseline → ±25% at 36mo)
    base_ci = 0.15
    horizon_factor = np.linspace(0, 0.10, months_ahead)
    lower = point * (1 - (base_ci + horizon_factor))
    upper = point * (1 + (base_ci + horizon_factor))

    return {
        "point": [float(v) for v in point],
        "lower": [float(v) for v in lower],
        "upper": [float(v) for v in upper],
        "confidence": confidence,
        "rmse_pct": round(rmse_pct, 1),
        "historical": list(historical),
    }


# ── chart ─────────────────────────────────────────────────────────────────
def build_forecast_chart(historical: List[float],
                         forecast_data: Optional[dict] = None,
                         title: str = "Price forecast 36 months",
                         currency_suffix: str = " AED") -> Optional[bytes]:
    """Render historical+forecast chart as PNG bytes. Returns None on failure."""
    if not MPL_OK or not NP_OK:
        return None
    if not historical or len(historical) < 2:
        return None

    fc = forecast_data or forecast_prices(historical, months_ahead=36)

    try:
        fig, ax = plt.subplots(figsize=(7.5, 3.4), dpi=110)
        n_hist = len(fc["historical"])
        n_fut = len(fc["point"])

        x_hist = np.arange(n_hist)
        x_fut = np.arange(n_hist, n_hist + n_fut)

        # historical solid gold
        ax.plot(x_hist, fc["historical"], color="#C9A84C",
                linewidth=2.2, label="Исторические данные")
        # forecast dashed
        ax.plot(x_fut, fc["point"], color="#1D4ED8",
                linewidth=2.0, linestyle="--", label="Прогноз")
        # CI band
        ax.fill_between(x_fut, fc["lower"], fc["upper"],
                        color="#1D4ED8", alpha=0.12,
                        label=f"Доверительный интервал (±15-25%)")

        # 12/24/36-mo annotations
        if n_fut >= 12:
            v12 = fc["point"][11]
            ci12 = (fc["upper"][11] - fc["lower"][11]) / 2 / v12 * 100 if v12 else 0
            ax.annotate(f"12м: ~{v12:,.0f} ±{ci12:.0f}%",
                        xy=(n_hist + 11, v12),
                        xytext=(n_hist + 11, v12 * 1.10),
                        fontsize=8, color="#0B1929", fontweight="bold")
        if n_fut >= 24:
            v24 = fc["point"][23]
            ax.annotate(f"24м: ~{v24:,.0f}",
                        xy=(n_hist + 23, v24),
                        xytext=(n_hist + 23, v24 * 1.07),
                        fontsize=8, color="#0B1929")
        if n_fut >= 36:
            v36 = fc["point"][35]
            ci36 = (fc["upper"][35] - fc["lower"][35]) / 2 / v36 * 100 if v36 else 0
            ax.annotate(f"36м: ~{v36:,.0f} ±{ci36:.0f}%",
                        xy=(n_hist + 35, v36),
                        xytext=(n_hist + 30, v36 * 1.12),
                        fontsize=8, color="#0B1929", fontweight="bold")

        # confidence badge
        conf_label = {"high": "Высокая уверенность",
                       "medium": "Средняя уверенность",
                       "low": "Низкая уверенность — мало истории"}[fc["confidence"]]
        conf_color = {"high": "#16A34A",
                       "medium": "#CA8A04",
                       "low": "#DC2626"}[fc["confidence"]]
        ax.text(0.01, 0.96, conf_label, transform=ax.transAxes,
                fontsize=8, color=conf_color, fontweight="bold",
                verticalalignment="top",
                bbox=dict(facecolor="white", edgecolor=conf_color, lw=0.7, pad=2))

        # vertical separator at "now"
        ax.axvline(n_hist - 0.5, color="#888888", linestyle=":", linewidth=0.8, alpha=0.7)

        ax.set_title(title, fontsize=10, color="#0B1929", fontweight="bold")
        ax.set_xlabel("Месяцы", fontsize=8)
        ax.set_ylabel(f"Цена{currency_suffix}", fontsize=8)
        ax.tick_params(axis="both", labelsize=7)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.grid(True, alpha=0.2, linestyle="--")
        ax.legend(loc="lower right", fontsize=7, frameon=False)

        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
        plt.close(fig)
        return buf.getvalue()
    except Exception as e:
        log.warning(f"build_forecast_chart failed: {e}")
        try:
            plt.close("all")
        except Exception:
            pass
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sample = [20000 + i * 250 + (i % 3) * 80 for i in range(24)]
    out = forecast_prices(sample, months_ahead=36)
    print(f"confidence={out['confidence']} rmse_pct={out['rmse_pct']}")
    print(f"12m={out['point'][11]:.0f}  24m={out['point'][23]:.0f}  36m={out['point'][35]:.0f}")
    png = build_forecast_chart(sample, out)
    if png:
        with open("_test_forecast.png", "wb") as f:
            f.write(png)
        print("OK -> _test_forecast.png")
