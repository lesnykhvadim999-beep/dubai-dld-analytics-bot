"""vadim_pdf.py — общий PDF-модуль Vadim Realty (10 страниц).

Унифицированный профессиональный PDF-отчёт для всех ботов:
  analytics-bot / channel-bot / roi-bot / resale-bot / lead-bot

Структура (10 страниц):
  1.  Обложка — лого, тип отчёта, BRN, дата
  2.  Executive summary (LLM Cerebras → Groq → … free-tier)
  3.  Детали объекта/района/проекта (KPI grid)
  4-5. DLD-аналитика — графики matplotlib (цена/m², dynamics, top-quartile)
  6.  ROI 5/10 лет (bar chart)
  7.  Сравнение с топ-10 похожих
  8.  Risk factors + Positive signals
  9.  Vadim profile (RERA BRN 65011, контакты)
  10. Disclaimer

Зависимости:
  reportlab>=4.0
  matplotlib>=3.7
  Pillow>=10.0

API:
  generate_pdf_report(report_type, payload, lang="ru", output_dir="/tmp") -> str

Кэш `pdf_reports(report_key, payload_hash, file_path, generated_at)`
ленивo создаётся в БД ($DATABASE_URL) при первом вызове.

Performance цель: < 5 сек на отчёт.

Бренд:
  «Vadim Realty · RERA Licensed Broker · BRN 65011 · Dubai»
  НИКОГДА не упоминать "First Place Realtor L.L.C." (см. memory feedback).
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import tempfile
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("vadim_pdf")

# ── Brand identity (CRITICAL: don't change without checking memory) ──
BRAND_NAME = "Vadim Realty"
BRAND_SUBTITLE = "RERA Licensed Broker · BRN 65011 · Dubai"
BRAND_BRN = "65011"
BRAND_CONTACT_TG = "@vadim_dubai_realty"
BRAND_CONTACT_PHONE = "+971 58 539 86 64"

# ── reportlab (lazy load – may not be installed in dev) ──
try:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm, mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.pdfmetrics import registerFontFamily
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph,
                                     SimpleDocTemplate, Spacer, Table,
                                     TableStyle)
    REPORTLAB_OK = True
except ImportError:
    REPORTLAB_OK = False
    log.warning("reportlab not installed — PDF generation disabled")

# ── matplotlib (only for charts) ──
try:
    import matplotlib
    matplotlib.use("Agg")  # headless
    import matplotlib.pyplot as plt
    MPL_OK = True
except ImportError:
    MPL_OK = False
    log.warning("matplotlib not installed — charts will be skipped")

# ── Pillow ──
try:
    from PIL import Image as PILImage
    PIL_OK = True
except ImportError:
    PIL_OK = False


# ── Brand palette (amber/stone, как на сайте Vadim Realty) ──
if REPORTLAB_OK:
    AMBER       = colors.HexColor("#B45309")   # amber-700 (deep)
    AMBER_LIGHT = colors.HexColor("#FCD34D")   # amber-300
    AMBER_FAINT = colors.HexColor("#FEF3C7")   # amber-100
    STONE_900   = colors.HexColor("#1C1917")   # almost-black
    STONE_700   = colors.HexColor("#44403C")   # body
    STONE_500   = colors.HexColor("#78716C")   # muted
    STONE_300   = colors.HexColor("#D6D3D1")   # borders
    STONE_100   = colors.HexColor("#F5F5F4")   # bg
    GREEN_OK    = colors.HexColor("#15803D")
    RED_BAD     = colors.HexColor("#B91C1C")
    BLUE_INFO   = colors.HexColor("#1D4ED8")


# ── Font registration ──
_FONT_REG = "DejaVuSans"
_FONT_BOLD = "DejaVuSans-Bold"


def _register_fonts():
    """Find DejaVu Sans .ttf next to this file or in bot 'fonts/' dir."""
    if not REPORTLAB_OK:
        return
    # already registered?
    try:
        if _FONT_REG in pdfmetrics.getRegisteredFontNames():
            return
    except Exception:
        pass

    candidates = []
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "fonts"))
    candidates.append(here)
    candidates.append(os.path.join(os.getcwd(), "fonts"))
    candidates.append(os.getcwd())
    # System fallback Linux
    candidates.append("/usr/share/fonts/truetype/dejavu")

    for d in candidates:
        reg = os.path.join(d, "DejaVuSans.ttf")
        bold = os.path.join(d, "DejaVuSans-Bold.ttf")
        if os.path.exists(reg):
            try:
                pdfmetrics.registerFont(TTFont(_FONT_REG, reg))
                if os.path.exists(bold):
                    pdfmetrics.registerFont(TTFont(_FONT_BOLD, bold))
                else:
                    pdfmetrics.registerFont(TTFont(_FONT_BOLD, reg))
                registerFontFamily(
                    _FONT_REG, normal=_FONT_REG, bold=_FONT_BOLD,
                    italic=_FONT_REG, boldItalic=_FONT_BOLD)
                log.info(f"PDF fonts registered from {d}")
                return
            except Exception as e:
                log.warning(f"font registration failed in {d}: {e}")
    log.warning("DejaVu Sans not found — falling back to Helvetica (no cyrillic)")


# ── Styles ──
def _styles():
    _register_fonts()
    s = getSampleStyleSheet()
    F = _FONT_REG if _FONT_REG in pdfmetrics.getRegisteredFontNames() else "Helvetica"
    FB = _FONT_BOLD if _FONT_BOLD in pdfmetrics.getRegisteredFontNames() else "Helvetica-Bold"
    return {
        "cover_brand": ParagraphStyle(
            "cover_brand", parent=s["Title"], fontSize=32, fontName=FB,
            textColor=AMBER, leading=38, alignment=TA_CENTER, spaceAfter=8),
        "cover_sub": ParagraphStyle(
            "cover_sub", parent=s["BodyText"], fontSize=12, fontName=F,
            textColor=STONE_500, leading=16, alignment=TA_CENTER, spaceAfter=4),
        "cover_title": ParagraphStyle(
            "cover_title", parent=s["Title"], fontSize=24, fontName=FB,
            textColor=STONE_900, leading=30, alignment=TA_CENTER, spaceAfter=16),
        "cover_meta": ParagraphStyle(
            "cover_meta", parent=s["BodyText"], fontSize=10, fontName=F,
            textColor=STONE_700, leading=14, alignment=TA_CENTER),
        "h1": ParagraphStyle(
            "h1", parent=s["Heading1"], fontSize=20, fontName=FB,
            textColor=AMBER, leading=24, alignment=TA_LEFT, spaceAfter=10, spaceBefore=2),
        "h2": ParagraphStyle(
            "h2", parent=s["Heading2"], fontSize=14, fontName=FB,
            textColor=STONE_900, leading=18, spaceBefore=10, spaceAfter=6),
        "h3": ParagraphStyle(
            "h3", parent=s["Heading3"], fontSize=11, fontName=FB,
            textColor=AMBER, leading=14, spaceAfter=2),
        "body": ParagraphStyle(
            "body", parent=s["BodyText"], fontSize=10, fontName=F,
            textColor=STONE_700, leading=15, spaceAfter=6, alignment=TA_JUSTIFY),
        "muted": ParagraphStyle(
            "muted", parent=s["BodyText"], fontSize=8, fontName=F,
            textColor=STONE_500, leading=11),
        "kpi_v": ParagraphStyle(
            "kpi_v", parent=s["BodyText"], fontSize=18, fontName=FB,
            textColor=STONE_900, leading=22, alignment=TA_CENTER, spaceAfter=2),
        "kpi_l": ParagraphStyle(
            "kpi_l", parent=s["BodyText"], fontSize=8, fontName=F,
            textColor=STONE_500, leading=10, alignment=TA_CENTER),
        "ok": ParagraphStyle(
            "ok", parent=s["BodyText"], fontSize=10, fontName=F,
            textColor=GREEN_OK, leading=14, spaceAfter=4, leftIndent=8),
        "bad": ParagraphStyle(
            "bad", parent=s["BodyText"], fontSize=10, fontName=F,
            textColor=RED_BAD, leading=14, spaceAfter=4, leftIndent=8),
        "disclaimer": ParagraphStyle(
            "disclaimer", parent=s["BodyText"], fontSize=8, fontName=F,
            textColor=STONE_500, leading=11, alignment=TA_JUSTIFY, spaceAfter=4),
    }


# ── Localization ──
I18N = {
    "ru": {
        "report_types": {
            "area":     "Аналитика района",
            "building": "Аналитика здания",
            "project":  "Investment Outlook (проект)",
            "listing":  "Инвестиционный анализ объекта",
            "roi":      "ROI расчёт",
            "lead":     "Заявка клиента",
        },
        "exec_summary":    "Исполнительное резюме",
        "details":         "Ключевые показатели",
        "dld_chart":       "DLD-аналитика · динамика 12 мес",
        "dld_dist":        "Распределение сделок (top-quartile)",
        "roi_chart":       "Прогноз ROI 5 / 10 лет",
        "comparison":      "Сравнение с топ-10 похожих",
        "risks":           "Риски и факторы внимания",
        "signals":         "Позитивные сигналы",
        "vadim_profile":   "О брокере",
        "disclaimer":      "Юридическая оговорка",
        "page":            "Страница",
        "of":              "из",
        "date":            "Дата",
        "no_data":         "Недостаточно данных для построения графика",
        "default_risks":   ["Курсовые колебания AED/RUB",
                            "Изменения регуляций DLD",
                            "Сроки сдачи off-plan могут смещаться"],
        "default_signals": ["RERA-лицензированная сделка",
                            "Vadim Realty работает только с проверенными застройщиками",
                            "Прозрачный escrow согласно DLD"],
        "summary_fallback":"Подробный отчёт по выбранному запросу. Все цифры взяты из официальной базы Dubai Pulse / DLD.",
        "vadim_bio":       ("Вадим — RERA-лицензированный брокер в Дубае (BRN 65011). "
                            "Специализация: жилая недвижимость, off-plan и вторичный рынок. "
                            "Все рекомендации основаны на данных Dubai Pulse / DLD и собственной аналитике."),
        "disclaimer_text": ("Данный отчёт носит информационный характер и не является публичной офертой или "
                            "индивидуальной инвестиционной рекомендацией. Все цифры рассчитаны на основе "
                            "публично доступных данных Dubai Land Department и могут расходиться с реальными "
                            "сделками. Прошлая доходность не гарантирует будущую. Vadim Realty не несёт "
                            "ответственности за решения, принятые на основе данного отчёта."),
    },
    "en": {
        "report_types": {
            "area":     "Area Analytics",
            "building": "Building Analytics",
            "project":  "Investment Outlook (Project)",
            "listing":  "Listing Investment Analysis",
            "roi":      "ROI Calculation",
            "lead":     "Client Inquiry",
        },
        "exec_summary":    "Executive Summary",
        "details":         "Key Indicators",
        "dld_chart":       "DLD Analytics · 12-month dynamics",
        "dld_dist":        "Deal distribution (top-quartile)",
        "roi_chart":       "ROI Forecast 5 / 10 years",
        "comparison":      "Comparison with Top-10 similar",
        "risks":           "Risk Factors",
        "signals":         "Positive Signals",
        "vadim_profile":   "About the Broker",
        "disclaimer":      "Legal Disclaimer",
        "page":            "Page",
        "of":              "of",
        "date":            "Date",
        "no_data":         "Not enough data to plot",
        "default_risks":   ["AED / FX fluctuations",
                            "Possible DLD regulation changes",
                            "Off-plan delivery dates may shift"],
        "default_signals": ["RERA-licensed transaction",
                            "Vadim Realty works only with vetted developers",
                            "Transparent DLD escrow"],
        "summary_fallback":"Detailed report for the selected query. All figures come from the official Dubai Pulse / DLD database.",
        "vadim_bio":       ("Vadim is a RERA-licensed broker in Dubai (BRN 65011). "
                            "Specialization: residential property, off-plan and secondary market. "
                            "All recommendations are based on Dubai Pulse / DLD data and proprietary analytics."),
        "disclaimer_text": ("This report is for informational purposes only and does not constitute a public offer "
                            "or individual investment advice. All figures are calculated from publicly available "
                            "Dubai Land Department data and may differ from actual transactions. Past performance "
                            "is not indicative of future results. Vadim Realty bears no responsibility for "
                            "decisions made based on this report."),
    },
    "ar": {
        "report_types": {
            "area":     "تحليلات المنطقة",
            "building": "تحليلات المبنى",
            "project":  "نظرة استثمارية (مشروع)",
            "listing":  "تحليل استثمار العقار",
            "roi":      "حساب العائد على الاستثمار",
            "lead":     "استفسار العميل",
        },
        "exec_summary":  "الملخص التنفيذي",
        "details":       "المؤشرات الرئيسية",
        "dld_chart":     "تحليلات DLD · ديناميات 12 شهر",
        "dld_dist":      "توزيع الصفقات",
        "roi_chart":     "توقعات ROI 5 / 10 سنوات",
        "comparison":    "المقارنة مع أفضل 10",
        "risks":         "عوامل الخطر",
        "signals":       "إشارات إيجابية",
        "vadim_profile": "عن الوسيط",
        "disclaimer":    "إخلاء المسؤولية القانونية",
        "page":          "صفحة",
        "of":            "من",
        "date":          "التاريخ",
        "no_data":       "لا توجد بيانات كافية",
        "default_risks":   ["تقلبات أسعار الصرف",
                            "تغييرات لوائح DLD المحتملة",
                            "قد تتغير تواريخ تسليم على الخارطة"],
        "default_signals": ["معاملة مرخصة من RERA",
                            "تعمل Vadim Realty مع المطورين الموثوقين فقط",
                            "ضمان شفاف من DLD"],
        "summary_fallback":"تقرير مفصل. تأتي جميع الأرقام من قاعدة بيانات DLD.",
        "vadim_bio":       "وديم وسيط مرخص من RERA في دبي (BRN 65011).",
        "disclaimer_text": "هذا التقرير لأغراض إعلامية فقط ولا يشكل عرضًا عامًا أو نصيحة استثمارية فردية.",
    },
}


def _t(lang: str, key: str, default: str = "") -> str:
    d = I18N.get(lang, I18N["en"])
    return d.get(key, default or key)


# ── DB cache ──
def _db_url() -> Optional[str]:
    for k in ("DATABASE_URL", "READ_MODEL_DATABASE_URL", "ANALYTICS_DATABASE_URL"):
        v = os.environ.get(k)
        if v:
            return v
    return None


def _ensure_cache_table():
    url = _db_url()
    if not url or "postgres" not in url.lower():
        return
    try:
        import psycopg
        with psycopg.connect(url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS pdf_reports (
                        report_key TEXT NOT NULL,
                        payload_hash TEXT NOT NULL,
                        file_path TEXT,
                        file_bytes BYTEA,
                        generated_at TIMESTAMPTZ DEFAULT NOW(),
                        PRIMARY KEY (report_key, payload_hash)
                    )
                """)
                conn.commit()
    except Exception as e:
        log.debug(f"pdf_reports cache table ensure failed: {e}")


def _cache_get(report_key: str, payload_hash: str) -> Optional[bytes]:
    url = _db_url()
    if not url or "postgres" not in url.lower():
        return None
    try:
        import psycopg
        with psycopg.connect(url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT file_bytes FROM pdf_reports WHERE report_key=%s AND payload_hash=%s",
                    (report_key, payload_hash))
                row = cur.fetchone()
                if row and row[0]:
                    return bytes(row[0])
    except Exception as e:
        log.debug(f"pdf_reports cache get failed: {e}")
    return None


def _cache_put(report_key: str, payload_hash: str, file_path: str, data: bytes):
    url = _db_url()
    if not url or "postgres" not in url.lower():
        return
    try:
        import psycopg
        with psycopg.connect(url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO pdf_reports (report_key, payload_hash, file_path, file_bytes, generated_at)
                    VALUES (%s, %s, %s, %s, NOW())
                    ON CONFLICT (report_key, payload_hash) DO UPDATE
                        SET file_path = EXCLUDED.file_path,
                            file_bytes = EXCLUDED.file_bytes,
                            generated_at = NOW()
                """, (report_key, payload_hash, file_path, data))
                conn.commit()
    except Exception as e:
        log.debug(f"pdf_reports cache put failed: {e}")


# ── LLM summary ──
def _llm_summary(payload: dict, lang: str = "ru") -> str:
    """Call free-tier LLM chain for 2-3 paragraph executive summary."""
    try:
        from llm_chain import llm_call  # type: ignore
    except Exception:
        return _t(lang, "summary_fallback")

    # Compact payload for prompt
    sample = {k: v for k, v in payload.items()
              if k not in ("photos", "raw_rows", "_internal") and v is not None}
    if len(json.dumps(sample, default=str)) > 3000:
        sample = {k: v for k, v in list(sample.items())[:20]}

    sys_by_lang = {
        "ru": ("Ты — RERA-лицензированный аналитик Vadim Realty в Дубае. "
               "Дай 2-3 коротких параграфа исполнительного резюме по данным ниже. "
               "Тон: профессиональный, без воды. Только факты, без рекламы. "
               "Не упоминай юр. лицо брокерской компании. БРЕНД: Vadim Realty."),
        "en": ("You are a RERA-licensed analyst at Vadim Realty, Dubai. "
               "Give 2-3 short paragraphs of executive summary based on the data below. "
               "Tone: professional, factual. Do not mention any brokerage legal entity. "
               "BRAND: Vadim Realty."),
        "ar": ("أنت محلل مرخص من RERA في Vadim Realty بدبي. "
               "اكتب 2-3 فقرات قصيرة كملخص تنفيذي بناءً على البيانات. "
               "العلامة التجارية: Vadim Realty."),
    }
    prompt = (
        sys_by_lang.get(lang, sys_by_lang["en"]) +
        "\n\nDATA:\n" + json.dumps(sample, ensure_ascii=False, default=str)[:2500] +
        "\n\nWrite 2-3 paragraphs:"
    )

    try:
        out = llm_call(prompt, max_tokens=400, timeout=12)
        if out:
            # Sanitize forbidden brokerage name
            out = out.replace("First Place Realtor L.L.C.", "Vadim Realty")
            out = out.replace("First Place Realty", "Vadim Realty")
            out = out.replace("First Place", "Vadim Realty")
            return out.strip()
    except Exception as e:
        log.warning(f"LLM summary failed: {e}")
    return _t(lang, "summary_fallback")


# ── Helpers ──
def _money(v: Any, suffix: str = " AED") -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    if v >= 1_000_000:
        return f"{v/1_000_000:.2f}M{suffix}"
    if v >= 1_000:
        return f"{v/1_000:.0f}K{suffix}"
    return f"{v:,.0f}{suffix}"


def _num(v: Any, suffix: str = "") -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    if abs(v - round(v)) < 0.001:
        return f"{int(v):,}{suffix}"
    return f"{v:,.2f}{suffix}"


def _pct(v: Any) -> str:
    try:
        return f"{float(v):.1f}%"
    except (TypeError, ValueError):
        return "—"


def _find_logo() -> Optional[str]:
    """Find any logo image next to bot main file."""
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("logo.png", "logo.jpg", "vadim_logo.png",
                 "logo_new_projects.png", "vadim_realty_logo.png"):
        p = os.path.join(here, name)
        if os.path.exists(p):
            return p
    # Try cwd
    for name in ("logo.png", "logo.jpg", "vadim_logo.png"):
        p = os.path.join(os.getcwd(), name)
        if os.path.exists(p):
            return p
    return None


# ── Chart builders (matplotlib → PNG bytes) ──
def _chart_dynamics(series: List[Tuple[str, float]], title: str, ylabel: str) -> Optional[bytes]:
    if not MPL_OK or not series:
        return None
    try:
        labels = [s[0] for s in series]
        values = [s[1] for s in series]
        fig, ax = plt.subplots(figsize=(7.2, 3.2), dpi=130)
        ax.plot(labels, values, color="#B45309", marker="o", linewidth=2.2)
        ax.fill_between(range(len(labels)), values, color="#FCD34D", alpha=0.25)
        ax.set_title(title, fontsize=11, color="#1C1917", pad=10)
        ax.set_ylabel(ylabel, fontsize=9, color="#44403C")
        ax.tick_params(axis="x", rotation=30, labelsize=8)
        ax.tick_params(axis="y", labelsize=8)
        ax.grid(True, axis="y", linestyle="--", alpha=0.35)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        plt.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
        plt.close(fig)
        return buf.getvalue()
    except Exception as e:
        log.warning(f"chart dynamics failed: {e}")
        return None


def _chart_bars(items: List[Tuple[str, float]], title: str, color: str = "#B45309") -> Optional[bytes]:
    if not MPL_OK or not items:
        return None
    try:
        labels = [str(i[0]) for i in items]
        values = [float(i[1]) for i in items]
        fig, ax = plt.subplots(figsize=(7.2, 3.2), dpi=130)
        bars = ax.bar(labels, values, color=color, edgecolor="#1C1917", linewidth=0.4)
        for b, v in zip(bars, values):
            ax.text(b.get_x() + b.get_width() / 2, b.get_height(),
                    f"{v:,.0f}", ha="center", va="bottom",
                    fontsize=8, color="#1C1917")
        ax.set_title(title, fontsize=11, color="#1C1917", pad=10)
        ax.tick_params(axis="x", rotation=20, labelsize=8)
        ax.tick_params(axis="y", labelsize=8)
        ax.grid(True, axis="y", linestyle="--", alpha=0.35)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        plt.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
        plt.close(fig)
        return buf.getvalue()
    except Exception as e:
        log.warning(f"chart bars failed: {e}")
        return None


def _chart_distribution(values: List[float], title: str) -> Optional[bytes]:
    if not MPL_OK or not values:
        return None
    try:
        fig, ax = plt.subplots(figsize=(7.2, 3.0), dpi=130)
        ax.hist(values, bins=12, color="#FCD34D", edgecolor="#B45309")
        ax.set_title(title, fontsize=11, color="#1C1917", pad=10)
        ax.tick_params(labelsize=8)
        ax.grid(True, axis="y", linestyle="--", alpha=0.35)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        plt.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
        plt.close(fig)
        return buf.getvalue()
    except Exception as e:
        log.warning(f"chart distribution failed: {e}")
        return None


def _png_image(data: bytes, width_cm: float = 16) -> Optional[Image]:
    if not data:
        return None
    try:
        buf = io.BytesIO(data)
        img = Image(buf, width=width_cm * cm, height=width_cm * cm * 0.45)
        img.hAlign = "CENTER"
        return img
    except Exception as e:
        log.warning(f"_png_image failed: {e}")
        return None


# ── Footer / Header callback for SimpleDocTemplate ──
def _make_footer(lang: str):
    def _draw(canvas, doc):
        canvas.saveState()
        canvas.setFont(_FONT_REG if _FONT_REG in pdfmetrics.getRegisteredFontNames() else "Helvetica", 8)
        canvas.setFillColor(STONE_500)
        # Footer left
        canvas.drawString(2 * cm, 1.2 * cm,
                          f"{BRAND_NAME} · BRN {BRAND_BRN}")
        # Footer right (page n of m)
        page = canvas.getPageNumber()
        canvas.drawRightString(A4[0] - 2 * cm, 1.2 * cm,
                               f"{_t(lang, 'page')} {page}")
        # Brand strip top
        canvas.setStrokeColor(AMBER)
        canvas.setLineWidth(1.5)
        canvas.line(2 * cm, A4[1] - 1.2 * cm, A4[0] - 2 * cm, A4[1] - 1.2 * cm)
        canvas.restoreState()
    return _draw


# ── Page builders ──
def _page_cover(story: list, st: dict, report_type: str, payload: dict, lang: str):
    story.append(Spacer(1, 2.0 * cm))
    logo = _find_logo()
    if logo and PIL_OK:
        try:
            im = PILImage.open(logo)
            w, h = im.size
            target_w = 6 * cm
            ratio = h / w
            img = Image(logo, width=target_w, height=target_w * ratio)
            img.hAlign = "CENTER"
            story.append(img)
        except Exception as e:
            log.debug(f"cover logo embed failed: {e}")
    story.append(Spacer(1, 0.6 * cm))
    story.append(Paragraph(BRAND_NAME, st["cover_brand"]))
    story.append(Paragraph(BRAND_SUBTITLE, st["cover_sub"]))
    story.append(Spacer(1, 1.4 * cm))

    title = I18N.get(lang, I18N["en"])["report_types"].get(
        report_type, report_type.capitalize())
    subject = (payload.get("name") or payload.get("title")
               or payload.get("area") or payload.get("project_name")
               or payload.get("building_name") or "").strip()
    story.append(Paragraph(title, st["cover_title"]))
    if subject:
        story.append(Paragraph(f"<b>{subject}</b>", st["cover_meta"]))
    story.append(Spacer(1, 0.6 * cm))
    today = datetime.utcnow().strftime("%d %B %Y")
    story.append(Paragraph(f"{_t(lang, 'date')}: {today}", st["cover_meta"]))
    story.append(Spacer(1, 3.0 * cm))
    story.append(Paragraph(
        f"<font color='#B45309'><b>RERA BRN {BRAND_BRN}</b></font>",
        st["cover_meta"]))
    story.append(PageBreak())


def _page_executive(story: list, st: dict, payload: dict, lang: str):
    story.append(Paragraph(_t(lang, "exec_summary"), st["h1"]))
    story.append(Spacer(1, 0.2 * cm))
    summary = payload.get("summary") or payload.get("llm_summary")
    if not summary:
        summary = _llm_summary(payload, lang)
    # Hard sanitize
    summary = (summary
               .replace("First Place Realtor L.L.C.", "Vadim Realty")
               .replace("First Place Realty", "Vadim Realty")
               .replace("First Place", "Vadim Realty"))
    for para in [p for p in summary.split("\n\n") if p.strip()]:
        story.append(Paragraph(para.strip().replace("\n", " "), st["body"]))
    story.append(PageBreak())


def _kpi_table(items: List[Tuple[str, str]], st: dict, cols: int = 3) -> Optional[Table]:
    if not items:
        return None
    # Pad to multiple of cols
    while len(items) % cols:
        items.append(("", ""))
    rows = []
    for i in range(0, len(items), cols):
        chunk = items[i:i + cols]
        rows.append([
            [Paragraph(v or "—", st["kpi_v"]), Paragraph(l, st["kpi_l"])]
            for (l, v) in chunk
        ])
    t = Table(rows, colWidths=[5.4 * cm] * cols, hAlign="CENTER")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), AMBER_FAINT),
        ("BOX", (0, 0), (-1, -1), 0.4, AMBER),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, STONE_300),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    return t


def _page_details(story: list, st: dict, payload: dict, lang: str):
    story.append(Paragraph(_t(lang, "details"), st["h1"]))
    kpis = payload.get("kpis") or []
    if not kpis:
        # auto-extract from common payload keys
        auto = []
        for label_key, getter in [
            ("Average price", lambda p: _money(p.get("avg_price"))),
            ("Median price", lambda p: _money(p.get("median_price"))),
            ("Price per m²", lambda p: _money(p.get("price_per_m2"), " AED/m²")),
            ("Deals (12m)", lambda p: _num(p.get("deals") or p.get("deals_12m"))),
            ("Rental yield", lambda p: _pct(p.get("yield"))),
            ("YoY growth", lambda p: _pct(p.get("growth_yoy"))),
            ("Liquidity", lambda p: _num(p.get("liquidity"))),
            ("Total units", lambda p: _num(p.get("total_units"))),
            ("Area (m²)", lambda p: _num(p.get("area_m2"), " m²")),
        ]:
            v = getter(payload)
            if v and v != "—":
                auto.append((label_key, v))
        kpis = auto
    if kpis:
        t = _kpi_table([(l, v) for l, v in kpis][:9], st, cols=3)
        if t:
            story.append(t)
            story.append(Spacer(1, 0.4 * cm))
    desc = payload.get("description") or payload.get("details_text")
    if desc:
        story.append(Paragraph(desc[:1500], st["body"]))
    story.append(PageBreak())


def _page_dld_charts(story: list, st: dict, payload: dict, lang: str):
    # Page 4: dynamics 12m
    story.append(Paragraph(_t(lang, "dld_chart"), st["h1"]))
    series = payload.get("dynamics_series") or payload.get("price_series") or []
    if series:
        png = _chart_dynamics(series, _t(lang, "dld_chart"), "AED / m²")
        img = _png_image(png) if png else None
        if img:
            story.append(img)
    else:
        story.append(Paragraph(_t(lang, "no_data"), st["muted"]))
    story.append(Spacer(1, 0.4 * cm))
    notes = payload.get("dld_notes")
    if notes:
        story.append(Paragraph(str(notes)[:1200], st["body"]))
    story.append(PageBreak())

    # Page 5: distribution + top-quartile
    story.append(Paragraph(_t(lang, "dld_dist"), st["h1"]))
    dist = payload.get("price_distribution") or []
    if dist:
        png = _chart_distribution([float(x) for x in dist], _t(lang, "dld_dist"))
        img = _png_image(png) if png else None
        if img:
            story.append(img)
    else:
        story.append(Paragraph(_t(lang, "no_data"), st["muted"]))
    story.append(Spacer(1, 0.4 * cm))

    extras = payload.get("extra_chart")
    if extras and isinstance(extras, list):
        png = _chart_bars(extras, "Top quartile distribution", "#15803D")
        img = _png_image(png) if png else None
        if img:
            story.append(img)
    story.append(PageBreak())


def _page_roi(story: list, st: dict, payload: dict, lang: str):
    story.append(Paragraph(_t(lang, "roi_chart"), st["h1"]))
    roi5 = payload.get("roi_5y")
    roi10 = payload.get("roi_10y")
    rows = []
    if roi5 is not None:
        rows.append(("5y", float(roi5)))
    if roi10 is not None:
        rows.append(("10y", float(roi10)))
    # Допускаем breakdown по годам
    bd = payload.get("roi_breakdown") or []
    if bd:
        rows = [(str(b.get("year") or b.get("label")), float(b.get("value") or 0))
                for b in bd if b.get("value") is not None][:10]
    if rows:
        png = _chart_bars(rows, _t(lang, "roi_chart"), "#B45309")
        img = _png_image(png) if png else None
        if img:
            story.append(img)
            story.append(Spacer(1, 0.4 * cm))
    # KPI line
    kpis = []
    if payload.get("yield"):
        kpis.append(("Rental yield", _pct(payload.get("yield"))))
    if payload.get("growth"):
        kpis.append(("Growth p.a.", _pct(payload.get("growth"))))
    if payload.get("budget"):
        kpis.append(("Budget", _money(payload.get("budget"))))
    if payload.get("monthly_rent"):
        kpis.append(("Monthly rent", _money(payload.get("monthly_rent"))))
    if kpis:
        t = _kpi_table(kpis[:6], st, cols=3)
        if t:
            story.append(t)
    if not rows and not kpis:
        story.append(Paragraph(_t(lang, "no_data"), st["muted"]))
    story.append(PageBreak())


def _page_comparison(story: list, st: dict, payload: dict, lang: str):
    story.append(Paragraph(_t(lang, "comparison"), st["h1"]))
    comp = payload.get("comparison") or payload.get("similar") or []
    if comp:
        head = ["#", "Name", "Price / m²", "Deals", "Yield"]
        rows = [head]
        for i, c in enumerate(comp[:10], 1):
            rows.append([
                str(i),
                str(c.get("name", "—"))[:32],
                _money(c.get("price_per_m2"), " AED/m²"),
                _num(c.get("deals")),
                _pct(c.get("yield")),
            ])
        t = Table(rows, colWidths=[1 * cm, 6 * cm, 4 * cm, 2.5 * cm, 2.5 * cm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), AMBER),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), _FONT_BOLD),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, AMBER_FAINT]),
            ("GRID", (0, 0), (-1, -1), 0.3, STONE_300),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("ALIGN", (1, 1), (1, -1), "LEFT"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(t)
    else:
        story.append(Paragraph(_t(lang, "no_data"), st["muted"]))
    story.append(PageBreak())


def _page_risks(story: list, st: dict, payload: dict, lang: str):
    story.append(Paragraph(_t(lang, "risks"), st["h1"]))
    risks = payload.get("risks") or _t(lang, "default_risks")
    if isinstance(risks, list):
        for r in risks[:8]:
            story.append(Paragraph(f"<font color='#B91C1C'>•</font> {r}", st["bad"]))
    story.append(Spacer(1, 0.6 * cm))
    story.append(Paragraph(_t(lang, "signals"), st["h2"]))
    signals = payload.get("signals") or _t(lang, "default_signals")
    if isinstance(signals, list):
        for s in signals[:8]:
            story.append(Paragraph(f"<font color='#15803D'>✓</font> {s}", st["ok"]))
    story.append(PageBreak())


def _page_vadim_profile(story: list, st: dict, payload: dict, lang: str):
    story.append(Paragraph(_t(lang, "vadim_profile"), st["h1"]))
    logo = _find_logo()
    cells = []
    if logo and PIL_OK:
        try:
            im = PILImage.open(logo)
            w, h = im.size
            target_w = 4 * cm
            ratio = h / w
            cells.append(Image(logo, width=target_w, height=target_w * ratio))
        except Exception:
            cells.append(Paragraph(BRAND_NAME, st["h2"]))
    else:
        cells.append(Paragraph(BRAND_NAME, st["h2"]))

    bio_html = (
        f"<b>Vadim · {BRAND_SUBTITLE}</b><br/><br/>"
        f"{_t(lang, 'vadim_bio')}<br/><br/>"
        f"<b>Telegram:</b> {BRAND_CONTACT_TG}<br/>"
        f"<b>Phone:</b> {BRAND_CONTACT_PHONE}<br/>"
        f"<b>RERA BRN:</b> {BRAND_BRN}"
    )
    cells.append(Paragraph(bio_html, st["body"]))
    t = Table([cells], colWidths=[5 * cm, 11.5 * cm])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.5, AMBER),
        ("BACKGROUND", (0, 0), (-1, -1), AMBER_FAINT),
        ("LEFTPADDING", (0, 0), (-1, -1), 14),
        ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ("TOPPADDING", (0, 0), (-1, -1), 14),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
    ]))
    story.append(t)
    story.append(Spacer(1, 0.6 * cm))
    badge = Paragraph(
        f"<font size=12 color='#B45309'><b>★ RERA Licensed Broker · BRN {BRAND_BRN} ★</b></font>",
        st["cover_meta"])
    story.append(badge)
    story.append(PageBreak())


def _page_disclaimer(story: list, st: dict, lang: str):
    story.append(Paragraph(_t(lang, "disclaimer"), st["h1"]))
    txt = _t(lang, "disclaimer_text")
    for para in txt.split("\n\n"):
        story.append(Paragraph(para.strip(), st["disclaimer"]))
    story.append(Spacer(1, 1 * cm))
    story.append(Paragraph(
        f"© {datetime.utcnow().year} {BRAND_NAME} · BRN {BRAND_BRN} · Dubai, UAE",
        st["muted"]))


# ── Main API ──
def generate_pdf_report(
    report_type: str,
    payload: dict,
    lang: str = "ru",
    output_dir: Optional[str] = None,
) -> str:
    """Generate a 10-page Vadim Realty PDF report.

    Args:
        report_type: 'area' / 'building' / 'project' / 'listing' / 'roi' / 'lead'
        payload: dict with report data (see I18N + helpers for expected keys)
        lang: 'ru' / 'en' / 'ar'
        output_dir: target directory (defaults to tempfile.gettempdir())

    Returns:
        absolute path to the generated PDF file (cached if same payload_hash).
    """
    if not REPORTLAB_OK:
        raise RuntimeError("reportlab not installed — cannot generate PDF")

    if output_dir is None:
        output_dir = tempfile.gettempdir()
    os.makedirs(output_dir, exist_ok=True)

    # ── Cache key ──
    payload_norm = json.dumps(payload, sort_keys=True, default=str)[:32000]
    payload_hash = hashlib.sha256(
        f"{report_type}|{lang}|{payload_norm}".encode("utf-8")).hexdigest()[:24]
    report_key = f"{report_type}:{lang}"
    file_name = f"vadim_{report_type}_{payload_hash}.pdf"
    file_path = os.path.join(output_dir, file_name)

    # try cache
    _ensure_cache_table()
    cached = _cache_get(report_key, payload_hash)
    if cached:
        try:
            with open(file_path, "wb") as f:
                f.write(cached)
            log.info(f"PDF cache hit: {file_path}")
            return file_path
        except Exception:
            pass
    if os.path.exists(file_path):
        return file_path

    # ── Build PDF ──
    t0 = time.time()
    st = _styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
        title=f"{BRAND_NAME} — {report_type}",
        author=BRAND_NAME,
        subject=I18N.get(lang, I18N["en"])["report_types"].get(report_type, report_type),
    )

    story: list = []
    _page_cover(story, st, report_type, payload, lang)         # 1
    _page_executive(story, st, payload, lang)                  # 2
    _page_details(story, st, payload, lang)                    # 3
    _page_dld_charts(story, st, payload, lang)                 # 4-5
    _page_roi(story, st, payload, lang)                        # 6
    _page_comparison(story, st, payload, lang)                 # 7
    _page_risks(story, st, payload, lang)                      # 8
    _page_vadim_profile(story, st, payload, lang)              # 9
    _page_disclaimer(story, st, lang)                          # 10

    footer = _make_footer(lang)
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    data = buf.getvalue()
    buf.close()

    with open(file_path, "wb") as f:
        f.write(data)

    _cache_put(report_key, payload_hash, file_path, data)
    log.info(f"PDF generated: {file_path}  ({time.time() - t0:.2f}s, {len(data)//1024} KB)")
    return file_path


# ── self-test ──
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sample = {
        "name": "Dubai Marina",
        "avg_price": 2_450_000, "median_price": 2_100_000,
        "price_per_m2": 24_500, "deals": 1234, "yield": 6.8,
        "growth_yoy": 9.2, "liquidity": 87, "area_m2": 87,
        "dynamics_series": [(f"M{i:02d}", 20000 + i * 350) for i in range(1, 13)],
        "price_distribution": [20000 + i * 80 for i in range(60)],
        "roi_5y": 38.5, "roi_10y": 95.4,
        "roi_breakdown": [{"year": "Y1", "value": 7}, {"year": "Y2", "value": 14},
                          {"year": "Y3", "value": 22}, {"year": "Y4", "value": 30},
                          {"year": "Y5", "value": 38}],
        "comparison": [
            {"name": "JBR", "price_per_m2": 22500, "deals": 980, "yield": 6.4},
            {"name": "JLT", "price_per_m2": 19800, "deals": 1450, "yield": 7.1},
            {"name": "Business Bay", "price_per_m2": 21000, "deals": 1320, "yield": 6.7},
        ],
        "risks": ["Off-plan delivery риск", "Volatility 12% YoY"],
        "signals": ["RERA escrow", "Top-3 liquidity Dubai"],
    }
    out = generate_pdf_report("area", sample, lang="ru", output_dir="./_test")
    print(f"OK -> {out}")
