"""vadim_pdf.py — общий PDF-модуль Vadim Realty (компакт-3-страницы).

Унифицированный профессиональный PDF-отчёт для всех ботов:
  analytics-bot / channel-bot / roi-bot / resale-bot / lead-bot

v133 (compact): 3 страницы вместо 10, вся информация сохранена.

Структура (3 страницы A4):
  1.  Cover + Executive summary + Vadim profile (logo, заголовок,
      LLM-резюме, фото/контакты, BRN)
  2.  KPI grid + DLD charts + ROI chart + Risks/Signals (2 колонки)
  3.  Сравнение top-3 + детальный ROI + юр-оговорка + footer

Зависимости:
  reportlab>=4.0
  matplotlib>=3.7
  Pillow>=10.0

API:
  generate_pdf_report(report_type, payload, lang="ru", output_dir="/tmp") -> str

Кэш `pdf_reports(report_key, payload_hash, file_path, generated_at)`
ленивo создаётся в БД ($DATABASE_URL) при первом вызове.

Performance цель: < 5 сек на отчёт, < 500KB файл.

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
    from reportlab.platypus import (Image, KeepInFrame, KeepTogether,
                                     PageBreak, Paragraph,
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


# ── Styles (compact, 3-page layout) ──
def _styles():
    _register_fonts()
    s = getSampleStyleSheet()
    F = _FONT_REG if _FONT_REG in pdfmetrics.getRegisteredFontNames() else "Helvetica"
    FB = _FONT_BOLD if _FONT_BOLD in pdfmetrics.getRegisteredFontNames() else "Helvetica-Bold"
    return {
        "F": F, "FB": FB,
        # cover / brand (page 1 header)
        "cover_brand": ParagraphStyle(
            "cover_brand", parent=s["Title"], fontSize=20, fontName=FB,
            textColor=AMBER, leading=24, alignment=TA_CENTER, spaceAfter=2),
        "cover_sub": ParagraphStyle(
            "cover_sub", parent=s["BodyText"], fontSize=8.5, fontName=F,
            textColor=STONE_500, leading=11, alignment=TA_CENTER, spaceAfter=2),
        # H1 для секций (компакт)
        "h1": ParagraphStyle(
            "h1", parent=s["Heading1"], fontSize=14, fontName=FB,
            textColor=AMBER, leading=17, alignment=TA_LEFT, spaceAfter=4, spaceBefore=2),
        "h2": ParagraphStyle(
            "h2", parent=s["Heading2"], fontSize=10, fontName=FB,
            textColor=STONE_900, leading=12, spaceBefore=4, spaceAfter=2),
        "h3": ParagraphStyle(
            "h3", parent=s["Heading3"], fontSize=9, fontName=FB,
            textColor=AMBER, leading=11, spaceAfter=1),
        # cover_title — название отчёта/проекта на page 1
        "cover_title": ParagraphStyle(
            "cover_title", parent=s["Title"], fontSize=15, fontName=FB,
            textColor=STONE_900, leading=18, alignment=TA_CENTER, spaceAfter=4),
        "cover_meta": ParagraphStyle(
            "cover_meta", parent=s["BodyText"], fontSize=8.5, fontName=F,
            textColor=STONE_700, leading=11, alignment=TA_CENTER),
        # body (компакт)
        "body": ParagraphStyle(
            "body", parent=s["BodyText"], fontSize=8.5, fontName=F,
            textColor=STONE_700, leading=11, spaceAfter=3, alignment=TA_JUSTIFY),
        "body_l": ParagraphStyle(
            "body_l", parent=s["BodyText"], fontSize=8.5, fontName=F,
            textColor=STONE_700, leading=11, spaceAfter=3, alignment=TA_LEFT),
        "muted": ParagraphStyle(
            "muted", parent=s["BodyText"], fontSize=7.5, fontName=F,
            textColor=STONE_500, leading=10),
        # KPI cells (компакт)
        "kpi_v": ParagraphStyle(
            "kpi_v", parent=s["BodyText"], fontSize=10, fontName=FB,
            textColor=STONE_900, leading=12, alignment=TA_CENTER, spaceAfter=0),
        "kpi_l": ParagraphStyle(
            "kpi_l", parent=s["BodyText"], fontSize=6.5, fontName=F,
            textColor=STONE_500, leading=8, alignment=TA_CENTER),
        # risk/positive bullets
        "ok": ParagraphStyle(
            "ok", parent=s["BodyText"], fontSize=8, fontName=F,
            textColor=GREEN_OK, leading=10, spaceAfter=2, leftIndent=6),
        "bad": ParagraphStyle(
            "bad", parent=s["BodyText"], fontSize=8, fontName=F,
            textColor=RED_BAD, leading=10, spaceAfter=2, leftIndent=6),
        # contact line (page 1 bottom)
        "contact": ParagraphStyle(
            "contact", parent=s["BodyText"], fontSize=8.5, fontName=F,
            textColor=STONE_700, leading=12, alignment=TA_LEFT),
        # disclaimer (тонкий шрифт)
        "disclaimer": ParagraphStyle(
            "disclaimer", parent=s["BodyText"], fontSize=7, fontName=F,
            textColor=STONE_500, leading=9.5, alignment=TA_JUSTIFY, spaceAfter=2),
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
        "dld_chart":       "DLD · динамика 12 мес",
        "dld_dist":        "Распределение сделок",
        "roi_chart":       "Прогноз ROI 5 / 10 лет",
        "comparison":      "Сравнение с похожими",
        "risks":           "Риски",
        "signals":         "Позитивные сигналы",
        "vadim_profile":   "О брокере",
        "disclaimer":      "Юридическая оговорка",
        "page":            "Страница",
        "of":              "из",
        "date":            "Дата",
        "no_data":         "Недостаточно данных",
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
        "dld_chart":       "DLD · 12-month dynamics",
        "dld_dist":        "Deal distribution",
        "roi_chart":       "ROI Forecast 5 / 10 years",
        "comparison":      "Comparison with similar",
        "risks":           "Risk Factors",
        "signals":         "Positive Signals",
        "vadim_profile":   "About the Broker",
        "disclaimer":      "Legal Disclaimer",
        "page":            "Page",
        "of":              "of",
        "date":            "Date",
        "no_data":         "Not enough data",
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
        "dld_chart":     "تحليلات DLD · 12 شهر",
        "dld_dist":      "توزيع الصفقات",
        "roi_chart":     "توقعات ROI 5 / 10 سنوات",
        "comparison":    "المقارنة",
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
    """Call free-tier LLM chain for compact 1-paragraph executive summary."""
    try:
        from llm_chain import llm_call  # type: ignore
    except Exception:
        return _t(lang, "summary_fallback")

    sample = {k: v for k, v in payload.items()
              if k not in ("photos", "raw_rows", "_internal") and v is not None}
    if len(json.dumps(sample, default=str)) > 3000:
        sample = {k: v for k, v in list(sample.items())[:20]}

    sys_by_lang = {
        "ru": ("Ты — RERA-лицензированный аналитик Vadim Realty в Дубае. "
               "Дай 1 короткий параграф (до 80 слов) исполнительного резюме по данным ниже. "
               "Тон: профессиональный, без воды. Только факты, без рекламы. "
               "Не упоминай юр. лицо брокерской компании. БРЕНД: Vadim Realty."),
        "en": ("You are a RERA-licensed analyst at Vadim Realty, Dubai. "
               "Give 1 short paragraph (≤80 words) of executive summary based on the data below. "
               "Tone: professional, factual. Do not mention any brokerage legal entity. "
               "BRAND: Vadim Realty."),
        "ar": ("أنت محلل مرخص من RERA في Vadim Realty بدبي. "
               "اكتب فقرة قصيرة (≤80 كلمة) كملخص تنفيذي. "
               "العلامة التجارية: Vadim Realty."),
    }
    prompt = (
        sys_by_lang.get(lang, sys_by_lang["en"]) +
        "\n\nDATA:\n" + json.dumps(sample, ensure_ascii=False, default=str)[:2500] +
        "\n\nWrite 1 paragraph (max 80 words):"
    )

    try:
        out = llm_call(prompt, max_tokens=220, timeout=12)
        if out:
            out = (out.replace("First Place Realtor L.L.C.", "Vadim Realty")
                      .replace("First Place Realty", "Vadim Realty")
                      .replace("First Place", "Vadim Realty"))
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
    for name in ("logo.png", "logo.jpg", "vadim_logo.png"):
        p = os.path.join(os.getcwd(), name)
        if os.path.exists(p):
            return p
    return None


def _find_vadim_photo() -> Optional[str]:
    """Find Vadim's portrait photo next to bot main file."""
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("vadim.jpg", "vadim.png", "vadim_photo.jpg",
                 "vadim_photo.png", "broker.jpg", "broker.png"):
        p = os.path.join(here, name)
        if os.path.exists(p):
            return p
    return None


# ── Chart builders (matplotlib → PNG bytes, compact) ──
def _chart_dynamics_compact(series: List[Tuple[str, float]], title: str,
                             ylabel: str) -> Optional[bytes]:
    """Compact 3×2 inch chart for 2-column layout on page 2."""
    if not MPL_OK or not series:
        return None
    try:
        labels = [s[0] for s in series]
        values = [float(s[1]) for s in series]
        fig, ax = plt.subplots(figsize=(3.2, 1.9), dpi=110)
        ax.plot(range(len(labels)), values, color="#B45309", marker="o",
                linewidth=1.6, markersize=3)
        ax.fill_between(range(len(labels)), values, color="#FCD34D", alpha=0.25)
        ax.set_ylabel(ylabel, fontsize=6.5, color="#44403C")
        # show every Nth label only
        step = max(1, len(labels) // 6)
        ax.set_xticks(range(0, len(labels), step))
        ax.set_xticklabels([labels[i] for i in range(0, len(labels), step)],
                           fontsize=6, rotation=25)
        ax.tick_params(axis="y", labelsize=6)
        ax.grid(True, axis="y", linestyle="--", alpha=0.3)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        plt.tight_layout(pad=0.3)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0.05)
        plt.close(fig)
        return buf.getvalue()
    except Exception as e:
        log.warning(f"chart dynamics compact failed: {e}")
        return None


def _chart_bars_compact(items: List[Tuple[str, float]], title: str,
                         color: str = "#B45309") -> Optional[bytes]:
    """Compact bar chart for 2-column layout."""
    if not MPL_OK or not items:
        return None
    try:
        labels = [str(i[0])[:8] for i in items]
        values = [float(i[1]) for i in items]
        fig, ax = plt.subplots(figsize=(3.2, 1.9), dpi=110)
        bars = ax.bar(labels, values, color=color, edgecolor="#1C1917", linewidth=0.3)
        for b, v in zip(bars, values):
            ax.text(b.get_x() + b.get_width() / 2, b.get_height(),
                    f"{v:,.0f}", ha="center", va="bottom",
                    fontsize=5.5, color="#1C1917")
        ax.tick_params(axis="x", rotation=15, labelsize=6)
        ax.tick_params(axis="y", labelsize=6)
        ax.grid(True, axis="y", linestyle="--", alpha=0.3)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        plt.tight_layout(pad=0.3)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0.05)
        plt.close(fig)
        return buf.getvalue()
    except Exception as e:
        log.warning(f"chart bars compact failed: {e}")
        return None


def _chart_distribution_compact(values: List[float], title: str) -> Optional[bytes]:
    if not MPL_OK or not values:
        return None
    try:
        fig, ax = plt.subplots(figsize=(3.2, 1.9), dpi=110)
        ax.hist(values, bins=12, color="#FCD34D", edgecolor="#B45309", linewidth=0.4)
        ax.tick_params(labelsize=6)
        ax.grid(True, axis="y", linestyle="--", alpha=0.3)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        plt.tight_layout(pad=0.3)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0.05)
        plt.close(fig)
        return buf.getvalue()
    except Exception as e:
        log.warning(f"chart distribution compact failed: {e}")
        return None


def _png_image(data: bytes, width_cm: float = 7.5,
                height_cm: Optional[float] = None) -> Optional[Image]:
    """Embed PNG bytes as a reportlab Image with explicit size."""
    if not data:
        return None
    try:
        buf = io.BytesIO(data)
        h = height_cm * cm if height_cm is not None else width_cm * cm * 0.62
        img = Image(buf, width=width_cm * cm, height=h)
        img.hAlign = "CENTER"
        return img
    except Exception as e:
        log.warning(f"_png_image failed: {e}")
        return None


# ── Page/footer decorations ──
def _make_footer(lang: str, total_pages: int = 3):
    def _draw(canvas, doc):
        canvas.saveState()
        canvas.setFont(_FONT_REG if _FONT_REG in pdfmetrics.getRegisteredFontNames() else "Helvetica", 7)
        canvas.setFillColor(STONE_500)
        canvas.drawString(1.4 * cm, 0.9 * cm,
                          f"{BRAND_NAME} · RERA BRN {BRAND_BRN} · Dubai")
        page = canvas.getPageNumber()
        canvas.drawRightString(A4[0] - 1.4 * cm, 0.9 * cm,
                               f"{_t(lang, 'page')} {page} {_t(lang, 'of')} {total_pages}")
        # amber strip top
        canvas.setStrokeColor(AMBER)
        canvas.setLineWidth(1.2)
        canvas.line(1.4 * cm, A4[1] - 1.0 * cm, A4[0] - 1.4 * cm, A4[1] - 1.0 * cm)
        # amber strip bottom (above footer text)
        canvas.setStrokeColor(AMBER_LIGHT)
        canvas.setLineWidth(0.6)
        canvas.line(1.4 * cm, 1.15 * cm, A4[0] - 1.4 * cm, 1.15 * cm)
        canvas.restoreState()
    return _draw


# ── Static executive template (LLM fallback) ──
def _static_executive_template(payload: dict, lang: str) -> str:
    """Содержательное резюме из payload без LLM, компакт-форма (≤4 предложения)."""
    name = (payload.get("name") or payload.get("project_name")
            or payload.get("title") or payload.get("area") or "").strip()
    area = (payload.get("area") or payload.get("location") or "").strip()
    dev  = (payload.get("developer") or "").strip()
    emirate = (payload.get("emirate") or "Dubai").strip()
    handover = payload.get("handover_date") or payload.get("completion_year") or ""
    stage = payload.get("stage") or payload.get("status") or ""
    bedrooms = payload.get("bedrooms_range") or payload.get("bedrooms") or ""
    price_from = (payload.get("price_from") or payload.get("min_price")
                  or payload.get("avg_price"))

    yld = payload.get("yield") or payload.get("rental_yield")
    growth = payload.get("growth_yoy") or payload.get("growth")
    deals = (payload.get("deals") or payload.get("deals_12m")
             or payload.get("tx_count_12m"))
    total_return_5y = payload.get("total_return_5y_pct")
    payback = payload.get("payback_years")

    parts: List[str] = []
    if lang == "ru":
        intro = []
        if name:
            if area and area.lower() != name.lower():
                intro.append(f"«{name}» — {area} ({emirate})")
            else:
                intro.append(f"«{name}» ({emirate})")
        elif area:
            intro.append(f"Район {area} ({emirate})")
        if dev:
            intro.append(f"застройщик: {dev}")
        if handover:
            intro.append(f"сдача {handover}")
        if stage:
            intro.append(stage)
        if bedrooms:
            intro.append(str(bedrooms))
        if price_from:
            intro.append(f"от {_money(price_from)}")
        if intro:
            parts.append("; ".join(intro) + ".")

        mkt = []
        if deals:
            mkt.append(f"за 12 мес зарегистрировано {_num(deals)} сделок DLD")
        if yld:
            mkt.append(f"средняя rental yield ~{_pct(yld)}")
        if growth is not None:
            try:
                g = float(growth)
                sign = "рост" if g >= 0 else "коррекция"
                mkt.append(f"{sign} YoY {_pct(abs(g))}")
            except Exception:
                pass
        if mkt:
            parts.append("Рынок: " + ", ".join(mkt) + ".")

        inv = []
        if total_return_5y is not None:
            inv.append(f"совокупная доходность 5 лет ~{_pct(total_return_5y)}")
        if payback:
            inv.append(f"окупаемость ~{payback} лет")
        if inv:
            parts.append("Инвестиции: " + ", ".join(inv) + ".")
    else:
        intro = []
        if name:
            if area and area.lower() != name.lower():
                intro.append(f"{name} — {area} ({emirate})")
            else:
                intro.append(f"{name} ({emirate})")
        elif area:
            intro.append(f"{area} ({emirate})")
        if dev:
            intro.append(f"developer: {dev}")
        if handover:
            intro.append(f"handover {handover}")
        if bedrooms:
            intro.append(str(bedrooms))
        if price_from:
            intro.append(f"from {_money(price_from)}")
        if intro:
            parts.append("; ".join(intro) + ".")

        mkt = []
        if deals:
            mkt.append(f"{_num(deals)} DLD deals (12m)")
        if yld:
            mkt.append(f"yield ~{_pct(yld)}")
        if growth is not None:
            try:
                g = float(growth)
                sign = "growth" if g >= 0 else "correction"
                mkt.append(f"YoY {sign} {_pct(abs(g))}")
            except Exception:
                pass
        if mkt:
            parts.append("Market: " + ", ".join(mkt) + ".")

        inv = []
        if total_return_5y is not None:
            inv.append(f"5y total return ~{_pct(total_return_5y)}")
        if payback:
            inv.append(f"payback ~{payback}y")
        if inv:
            parts.append("Investment: " + ", ".join(inv) + ".")

    if not parts:
        return _t(lang, "summary_fallback")
    return " ".join(parts)


# ── KPI table builder (compact) ──
def _kpi_table_compact(items: List[Tuple[str, str]], st: dict,
                        cols: int = 2, col_width_cm: float = 4.4) -> Optional[Table]:
    """Build a compact KPI grid for left column (page 2)."""
    if not items:
        return None
    while len(items) % cols:
        items.append(("", ""))
    rows = []
    for i in range(0, len(items), cols):
        chunk = items[i:i + cols]
        rows.append([
            [Paragraph(v or "—", st["kpi_v"]), Paragraph(l, st["kpi_l"])]
            for (l, v) in chunk
        ])
    t = Table(rows, colWidths=[col_width_cm * cm] * cols, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), AMBER_FAINT),
        ("BOX", (0, 0), (-1, -1), 0.3, AMBER),
        ("INNERGRID", (0, 0), (-1, -1), 0.2, STONE_300),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]))
    return t


# ── Page 1: Cover + Executive + Profile ──
def _build_page1(story: list, st: dict, report_type: str,
                 payload: dict, lang: str):
    """Page 1: brand header + report title + executive paragraph + profile strip."""
    # ── Brand header (logo + name) — compact ──
    logo = _find_logo()
    if logo and PIL_OK:
        try:
            im = PILImage.open(logo)
            w, h = im.size
            target_w = 3.5 * cm
            ratio = h / w
            img = Image(logo, width=target_w, height=target_w * ratio)
            img.hAlign = "CENTER"
            story.append(img)
            story.append(Spacer(1, 0.15 * cm))
        except Exception as e:
            log.debug(f"cover logo embed failed: {e}")
    story.append(Paragraph(BRAND_NAME, st["cover_brand"]))
    story.append(Paragraph(BRAND_SUBTITLE, st["cover_sub"]))
    story.append(Spacer(1, 0.35 * cm))

    # ── Report title block ──
    title = I18N.get(lang, I18N["en"])["report_types"].get(
        report_type, report_type.capitalize())
    subject = (payload.get("name") or payload.get("title")
               or payload.get("area") or payload.get("project_name")
               or payload.get("building_name") or "").strip()
    area_ext = (payload.get("area") or payload.get("location") or "").strip()
    emirate = (payload.get("emirate") or "").strip()

    story.append(Paragraph(title, st["h3"]))
    if subject:
        story.append(Paragraph(f"<b>{subject}</b>", st["cover_title"]))
    sub_line_parts = []
    if area_ext and area_ext.lower() != subject.lower():
        sub_line_parts.append(area_ext)
    if emirate:
        sub_line_parts.append(emirate)
    today = datetime.utcnow().strftime("%d %b %Y")
    sub_line_parts.append(today)
    story.append(Paragraph(" · ".join(sub_line_parts), st["cover_meta"]))
    story.append(Spacer(1, 0.4 * cm))

    # ── Executive Summary section ──
    story.append(Paragraph(_t(lang, "exec_summary"), st["h1"]))
    summary = payload.get("summary") or payload.get("llm_summary")
    if not summary:
        try:
            llm_out = _llm_summary(payload, lang)
        except Exception:
            llm_out = None
        generic = _t(lang, "summary_fallback")
        if (not llm_out) or llm_out.strip() == generic.strip() or len(llm_out.strip()) < 80:
            summary = _static_executive_template(payload, lang)
        else:
            summary = llm_out
    summary = (summary
               .replace("First Place Realtor L.L.C.", "Vadim Realty")
               .replace("First Place Realty", "Vadim Realty")
               .replace("First Place", "Vadim Realty"))
    # Trim summary to ~700 chars to keep page 1 contained (preserves all info
    # since detailed KPI go to page 2 and disclaimer/comparison to page 3).
    summary_clean = summary.strip().replace("\n\n", " ").replace("\n", " ")
    if len(summary_clean) > 900:
        summary_clean = summary_clean[:880].rsplit(" ", 1)[0] + "…"
    story.append(Paragraph(summary_clean, st["body"]))

    # Optional: a project description if present and not already in summary
    desc = payload.get("description") or payload.get("details_text") or ""
    if desc and str(desc).strip() and str(desc).strip()[:60] not in summary_clean:
        d = str(desc).strip().replace("\n\n", " ").replace("\n", " ")
        if len(d) > 500:
            d = d[:480].rsplit(" ", 1)[0] + "…"
        story.append(Spacer(1, 0.2 * cm))
        story.append(Paragraph(d, st["body"]))

    # ── Vadim profile strip (bottom of page 1) ──
    story.append(Spacer(1, 0.5 * cm))
    # Use a 2-col table: photo (or logo) + contact block
    profile_left = None
    photo = _find_vadim_photo()
    if photo and PIL_OK:
        try:
            im = PILImage.open(photo)
            w, h = im.size
            target_w = 2.8 * cm
            ratio = h / w
            profile_left = Image(photo, width=target_w, height=target_w * ratio)
        except Exception:
            profile_left = None
    if profile_left is None and logo and PIL_OK:
        try:
            im = PILImage.open(logo)
            w, h = im.size
            target_w = 2.8 * cm
            ratio = h / w
            profile_left = Image(logo, width=target_w, height=target_w * ratio)
        except Exception:
            profile_left = Paragraph(f"<b>{BRAND_NAME}</b>", st["body_l"])
    if profile_left is None:
        profile_left = Paragraph(f"<b>{BRAND_NAME}</b>", st["body_l"])

    contact_html = (
        f"<b>Vadim · {BRAND_SUBTITLE}</b><br/>"
        f"{_t(lang, 'vadim_bio')}<br/>"
        f"<b>Telegram:</b> {BRAND_CONTACT_TG}   "
        f"<b>Phone:</b> {BRAND_CONTACT_PHONE}   "
        f"<b>RERA BRN:</b> {BRAND_BRN}"
    )
    profile_right = Paragraph(contact_html, st["contact"])

    profile_tbl = Table([[profile_left, profile_right]],
                        colWidths=[3.2 * cm, 14.6 * cm])
    profile_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOX", (0, 0), (-1, -1), 0.5, AMBER),
        ("BACKGROUND", (0, 0), (-1, -1), AMBER_FAINT),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(profile_tbl)
    story.append(PageBreak())


# ── Page 2: KPI grid + charts + risks/signals (2 columns) ──
def _build_page2(story: list, st: dict, payload: dict, lang: str):
    """Page 2: 2-column layout — left KPI+risks+signals, right charts."""
    story.append(Paragraph(_t(lang, "details"), st["h1"]))

    # ── Build LEFT column content (KPI + risks + signals) ──
    LABELS = {
        "ru": {
            "avg_price":  "Средняя цена",
            "median_price": "Медиана",
            "price_per_m2": "Цена за м²",
            "deals":      "Сделок (12m)",
            "yield":      "Rental yield",
            "growth":     "Рост YoY",
            "liquidity":  "Ликвидность",
            "units":      "Юнитов",
            "area":       "Площадь",
            "developer":  "Застройщик",
            "handover":   "Сдача",
            "bedrooms":   "Спальни",
            "emirate":    "Эмират",
            "stage":      "Стадия",
            "score":      "Инв. score",
            "payback":    "Окупаемость",
            "return_5y":  "Доход 5y",
            "top_psf":    "Top-quartile PSF",
            "area_growth_5y":  "Рост 5y",
            "area_growth_10y": "Рост 10y",
            "monthly_rent":    "Аренда/мес",
            "budget":          "Бюджет",
        },
        "en": {
            "avg_price":  "Avg price",
            "median_price": "Median",
            "price_per_m2": "Price / m²",
            "deals":      "Deals (12m)",
            "yield":      "Rental yield",
            "growth":     "YoY growth",
            "liquidity":  "Liquidity",
            "units":      "Units",
            "area":       "Area",
            "developer":  "Developer",
            "handover":   "Handover",
            "bedrooms":   "Bedrooms",
            "emirate":    "Emirate",
            "stage":      "Stage",
            "score":      "Inv. score",
            "payback":    "Payback",
            "return_5y":  "5y return",
            "top_psf":    "Top-q PSF",
            "area_growth_5y":  "Growth 5y",
            "area_growth_10y": "Growth 10y",
            "monthly_rent":    "Rent/mo",
            "budget":          "Budget",
        },
    }
    L = LABELS.get(lang, LABELS["en"])

    kpis = payload.get("kpis") or []
    if not kpis:
        auto: List[Tuple[str, str]] = []
        def add(label, value):
            if value and value != "—":
                auto.append((label, value))
        add(L["avg_price"],     _money(payload.get("avg_price")))
        add(L["median_price"],  _money(payload.get("median_price")))
        add(L["price_per_m2"],  _money(payload.get("price_per_m2"), " AED/m²"))
        add(L["top_psf"],       _money(payload.get("area_top_psf"), " AED/ft²") if payload.get("area_top_psf") else None)
        add(L["deals"],         _num(payload.get("deals") or payload.get("deals_12m") or payload.get("tx_count_12m")))
        add(L["yield"],         _pct(payload.get("yield") or payload.get("rental_yield")))
        add(L["growth"],        _pct(payload.get("growth_yoy") or payload.get("growth")))
        add(L["return_5y"],     _pct(payload.get("total_return_5y_pct")))
        add(L["payback"],       (f"{payload.get('payback_years')} лет" if lang == "ru" and payload.get("payback_years")
                                 else (f"{payload.get('payback_years')}y" if payload.get("payback_years") else None)))
        add(L["monthly_rent"],  _money(payload.get("monthly_rent")) if payload.get("monthly_rent") else None)
        add(L["budget"],        _money(payload.get("budget")) if payload.get("budget") else None)
        add(L["units"],         _num(payload.get("total_units") or payload.get("units")))
        add(L["area"],          (_num(payload.get("area_m2"), " m²") if payload.get("area_m2") else None))
        add(L["developer"],     str(payload.get("developer") or "")[:18] or None)
        add(L["handover"],      str(payload.get("handover_date") or payload.get("completion_year") or "")[:14] or None)
        add(L["bedrooms"],      str(payload.get("bedrooms_range") or payload.get("bedrooms") or "")[:14] or None)
        add(L["emirate"],       str(payload.get("emirate") or "")[:14] or None)
        add(L["stage"],         str(payload.get("stage") or payload.get("status") or "")[:14] or None)
        add(L["score"],         (str(payload.get("investment_score")) if payload.get("investment_score") is not None else None))
        add(L["area_growth_5y"],  _pct(payload.get("area_growth_5y")) if payload.get("area_growth_5y") is not None else None)
        add(L["area_growth_10y"], _pct(payload.get("area_growth_10y")) if payload.get("area_growth_10y") is not None else None)
        kpis = auto

    # Cap to 12 KPIs (2 cols × 6 rows) so left column fits next to charts
    kpis = kpis[:12]

    left_col: list = []
    kpi_tbl = _kpi_table_compact(kpis, st, cols=2, col_width_cm=4.2)
    if kpi_tbl:
        left_col.append(kpi_tbl)
        left_col.append(Spacer(1, 0.2 * cm))

    # Risks
    risks = payload.get("risks") or _t(lang, "default_risks")
    if isinstance(risks, list) and risks:
        left_col.append(Paragraph(_t(lang, "risks"), st["h2"]))
        for r in risks[:5]:
            left_col.append(Paragraph(f"<font color='#B91C1C'>•</font> {r}", st["bad"]))
        left_col.append(Spacer(1, 0.1 * cm))

    # Signals
    signals = payload.get("signals") or _t(lang, "default_signals")
    if isinstance(signals, list) and signals:
        left_col.append(Paragraph(_t(lang, "signals"), st["h2"]))
        for s_ in signals[:5]:
            left_col.append(Paragraph(f"<font color='#15803D'>✓</font> {s_}", st["ok"]))

    # ── Build RIGHT column content (charts) ──
    right_col: list = []
    series = payload.get("dynamics_series") or payload.get("price_series") or []
    dist = payload.get("price_distribution") or []
    notes = payload.get("dld_notes")
    extras = payload.get("extra_chart")

    # ROI rows
    roi5 = payload.get("roi_5y")
    roi10 = payload.get("roi_10y")
    roi_rows = []
    if roi5 is not None:
        roi_rows.append(("5y", float(roi5)))
    if roi10 is not None:
        roi_rows.append(("10y", float(roi10)))
    bd = payload.get("roi_breakdown") or []
    if bd:
        roi_rows = [(str(b.get("year") or b.get("label")), float(b.get("value") or 0))
                    for b in bd if b.get("value") is not None][:8]

    # Chart 1: dynamics
    if series:
        right_col.append(Paragraph(_t(lang, "dld_chart"), st["h2"]))
        png = _chart_dynamics_compact(series, _t(lang, "dld_chart"), "AED/m²")
        img = _png_image(png, width_cm=8.0, height_cm=4.6) if png else None
        if img:
            right_col.append(img)
        right_col.append(Spacer(1, 0.1 * cm))

    # Chart 2: distribution OR extras
    if dist:
        right_col.append(Paragraph(_t(lang, "dld_dist"), st["h2"]))
        png = _chart_distribution_compact([float(x) for x in dist], _t(lang, "dld_dist"))
        img = _png_image(png, width_cm=8.0, height_cm=4.6) if png else None
        if img:
            right_col.append(img)
        right_col.append(Spacer(1, 0.1 * cm))
    elif extras and isinstance(extras, list):
        right_col.append(Paragraph(_t(lang, "dld_dist"), st["h2"]))
        png = _chart_bars_compact(extras, "Top quartile", "#15803D")
        img = _png_image(png, width_cm=8.0, height_cm=4.6) if png else None
        if img:
            right_col.append(img)
        right_col.append(Spacer(1, 0.1 * cm))

    # Chart 3: ROI
    if roi_rows:
        right_col.append(Paragraph(_t(lang, "roi_chart"), st["h2"]))
        png = _chart_bars_compact(roi_rows, _t(lang, "roi_chart"), "#B45309")
        img = _png_image(png, width_cm=8.0, height_cm=4.6) if png else None
        if img:
            right_col.append(img)

    # If no charts at all — show notes/fallback
    if not right_col:
        right_col.append(Paragraph(_t(lang, "dld_chart"), st["h2"]))
        msg = ("Историческая 12-мес динамика недоступна для off-plan проектов "
               "до старта вторичного рынка. KPI слева — агрегаты по району "
               "и сравнимым активам из публичных данных DLD."
               if lang == "ru" else
               "12-month price dynamics is not yet available for off-plan units "
               "prior to the secondary-market launch. KPI on the left are "
               "area-level aggregates from public DLD data.")
        right_col.append(Paragraph(msg, st["muted"]))

    if notes:
        right_col.append(Spacer(1, 0.15 * cm))
        right_col.append(Paragraph(str(notes)[:600], st["muted"]))

    # ── Pack as 2-column table ──
    left_kif = KeepInFrame(8.4 * cm, 21 * cm, left_col, mode="shrink")
    right_kif = KeepInFrame(8.4 * cm, 21 * cm, right_col, mode="shrink")
    layout_tbl = Table([[left_kif, right_kif]],
                       colWidths=[8.7 * cm, 9.1 * cm])
    layout_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(layout_tbl)
    story.append(PageBreak())


# ── Page 3: Comparison + detailed ROI + disclaimer ──
def _build_page3(story: list, st: dict, payload: dict, lang: str):
    """Page 3: comparison table + detailed ROI breakdown + legal disclaimer."""
    story.append(Paragraph(_t(lang, "comparison"), st["h1"]))
    comp = payload.get("comparison") or payload.get("similar") or []
    if comp:
        if lang == "ru":
            head = ["#", "Название", "Район", "Цена/м²", "Сделок", "Yield", "Рост"]
        else:
            head = ["#", "Name", "Area", "Price/m²", "Deals", "Yield", "Growth"]
        rows = [head]
        for i, c in enumerate(comp[:10], 1):
            rows.append([
                str(i),
                str(c.get("name", "—"))[:24],
                str(c.get("area", "") or c.get("location", ""))[:14],
                _money(c.get("price_per_m2"), " AED/m²"),
                _num(c.get("deals")),
                _pct(c.get("yield")),
                _pct(c.get("growth") or c.get("growth_yoy")),
            ])
        t = Table(rows, colWidths=[0.8 * cm, 4.3 * cm, 2.9 * cm,
                                    3.2 * cm, 2.0 * cm, 1.8 * cm, 1.8 * cm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), AMBER),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), st["FB"]),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, AMBER_FAINT]),
            ("GRID", (0, 0), (-1, -1), 0.3, STONE_300),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("ALIGN", (1, 1), (1, -1), "LEFT"),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(t)
    else:
        area = (payload.get("area") or payload.get("location") or "").strip()
        avg = payload.get("avg_price") or payload.get("area_avg_price")
        psf = payload.get("price_per_m2") or payload.get("area_median_psf")
        yld = payload.get("yield") or payload.get("rental_yield")
        if lang == "ru":
            lines = []
            if area:
                lines.append(f"<b>{area}</b> — район Dubai, активный сегмент off-plan / secondary.")
            if avg:
                lines.append(f"Средняя цена сделок в районе: {_money(avg)}.")
            if psf:
                lines.append(f"Цена за м² (медиана района): {_money(psf, ' AED/m²')}.")
            if yld:
                lines.append(f"Типичная rental yield: {_pct(yld)}.")
            lines.append("Прямое сравнение с похожими проектами доступно для активов, "
                         "имеющих вторичный трек; для большинства off-plan проектов это "
                         f"сравнение появляется после handover. Подбор: {BRAND_CONTACT_TG}.")
        else:
            lines = []
            if area:
                lines.append(f"<b>{area}</b> — active Dubai sub-market across off-plan and secondary segments.")
            if avg:
                lines.append(f"Average area deal: {_money(avg)}.")
            if psf:
                lines.append(f"Median price per m²: {_money(psf, ' AED/m²')}.")
            if yld:
                lines.append(f"Typical rental yield: {_pct(yld)}.")
            lines.append("Side-by-side comparison is available for assets with a secondary-market "
                         "track record. For most off-plan projects this comparison forms after "
                         f"handover. Shortlist: {BRAND_CONTACT_TG}.")
        for ln in lines:
            story.append(Paragraph(ln, st["body"]))

    story.append(Spacer(1, 0.3 * cm))

    # ── Detailed ROI projection (table form) ──
    bd = payload.get("roi_breakdown") or []
    roi5 = payload.get("roi_5y")
    roi10 = payload.get("roi_10y")
    has_roi = bool(bd) or (roi5 is not None) or (roi10 is not None) or payload.get("total_return_5y_pct")

    if has_roi:
        story.append(Paragraph(_t(lang, "roi_chart"), st["h2"]))
        if bd:
            if lang == "ru":
                head = ["Год", "Доходность (%)"]
            else:
                head = ["Year", "Return (%)"]
            rows = [head]
            for b in bd[:10]:
                lab = str(b.get("year") or b.get("label") or "—")
                val = b.get("value")
                try:
                    val_s = f"{float(val):.1f}%"
                except Exception:
                    val_s = "—"
                rows.append([lab, val_s])
            t = Table(rows, colWidths=[2.5 * cm, 4 * cm], hAlign="LEFT")
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), AMBER),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), st["FB"]),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, AMBER_FAINT]),
                ("GRID", (0, 0), (-1, -1), 0.3, STONE_300),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]))
            story.append(t)
        else:
            parts = []
            if roi5 is not None:
                parts.append(f"5y: {float(roi5):.1f}%")
            if roi10 is not None:
                parts.append(f"10y: {float(roi10):.1f}%")
            if payload.get("total_return_5y_pct") is not None:
                parts.append(
                    ("Совокупная 5y: " if lang == "ru" else "Total 5y: ")
                    + _pct(payload.get("total_return_5y_pct")))
            if payload.get("payback_years"):
                parts.append(
                    ("Окупаемость ~" if lang == "ru" else "Payback ~")
                    + f"{payload.get('payback_years')}"
                    + (" лет" if lang == "ru" else "y"))
            if parts:
                story.append(Paragraph(" · ".join(parts), st["body"]))

    # ── Top buildings or extra ranking (если есть в payload) ──
    tb = payload.get("top_buildings") or []
    if tb and isinstance(tb, list):
        story.append(Spacer(1, 0.25 * cm))
        story.append(Paragraph(
            "Топ зданий района" if lang == "ru" else "Top buildings in area",
            st["h2"]))
        head = (["#", "Здание", "Цена/м²", "Сделок"] if lang == "ru"
                else ["#", "Building", "Price/m²", "Deals"])
        rows = [head]
        for i, b in enumerate(tb[:6], 1):
            rows.append([
                str(i),
                str(b.get("name") or b.get("building") or "—")[:30],
                _money(b.get("price_per_m2"), " AED/m²"),
                _num(b.get("deals")),
            ])
        t = Table(rows, colWidths=[0.8 * cm, 7 * cm, 3.5 * cm, 2.5 * cm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), AMBER),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), st["FB"]),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, AMBER_FAINT]),
            ("GRID", (0, 0), (-1, -1), 0.3, STONE_300),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("ALIGN", (1, 1), (1, -1), "LEFT"),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        story.append(t)

    # ── Disclaimer (компакт) ──
    story.append(Spacer(1, 0.5 * cm))
    story.append(Paragraph(_t(lang, "disclaimer"), st["h2"]))
    txt = _t(lang, "disclaimer_text")
    for para in txt.split("\n\n"):
        story.append(Paragraph(para.strip(), st["disclaimer"]))
    story.append(Spacer(1, 0.2 * cm))
    story.append(Paragraph(
        f"© {datetime.utcnow().year} {BRAND_NAME} · BRN {BRAND_BRN} · Dubai, UAE · "
        f"{BRAND_CONTACT_TG} · {BRAND_CONTACT_PHONE}",
        st["muted"]))
    # no PageBreak — last page


# ── Main API ──
def generate_pdf_report(
    report_type: str,
    payload: dict,
    lang: str = "ru",
    output_dir: Optional[str] = None,
) -> str:
    """Generate a compact 3-page Vadim Realty PDF report.

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
        f"{report_type}|{lang}|{payload_norm}|v133".encode("utf-8")).hexdigest()[:24]
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
        leftMargin=1.4 * cm, rightMargin=1.4 * cm,
        topMargin=1.3 * cm, bottomMargin=1.4 * cm,
        title=f"{BRAND_NAME} — {report_type}",
        author=BRAND_NAME,
        subject=I18N.get(lang, I18N["en"])["report_types"].get(report_type, report_type),
    )

    story: list = []
    _build_page1(story, st, report_type, payload, lang)   # cover + executive + profile
    _build_page2(story, st, payload, lang)                # KPI + charts + risks/signals
    _build_page3(story, st, payload, lang)                # comparison + ROI + disclaimer

    footer = _make_footer(lang, total_pages=3)
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
        "area": "Dubai Marina",
        "emirate": "Dubai",
        "developer": "Emaar Properties",
        "handover_date": "Q4 2026",
        "stage": "Off-plan",
        "bedrooms_range": "1-3 BR",
        "avg_price": 2_450_000, "median_price": 2_100_000,
        "price_per_m2": 24_500, "deals": 1234, "yield": 6.8,
        "growth_yoy": 9.2, "liquidity": 87, "area_m2": 87,
        "total_return_5y_pct": 38.5, "payback_years": 11,
        "area_growth_5y": 32, "area_growth_10y": 71,
        "monthly_rent": 14000, "budget": 2500000,
        "dynamics_series": [(f"M{i:02d}", 20000 + i * 350) for i in range(1, 13)],
        "price_distribution": [20000 + i * 80 for i in range(60)],
        "roi_5y": 38.5, "roi_10y": 95.4,
        "roi_breakdown": [{"year": "Y1", "value": 7}, {"year": "Y2", "value": 14},
                          {"year": "Y3", "value": 22}, {"year": "Y4", "value": 30},
                          {"year": "Y5", "value": 38}],
        "comparison": [
            {"name": "JBR", "area": "JBR", "price_per_m2": 22500, "deals": 980, "yield": 6.4, "growth": 7.2},
            {"name": "JLT", "area": "JLT", "price_per_m2": 19800, "deals": 1450, "yield": 7.1, "growth": 8.5},
            {"name": "Business Bay", "area": "Business Bay", "price_per_m2": 21000, "deals": 1320, "yield": 6.7, "growth": 9.0},
        ],
        "top_buildings": [
            {"name": "Marina Gate 1", "price_per_m2": 25000, "deals": 120},
            {"name": "Marina Promenade", "price_per_m2": 23500, "deals": 95},
            {"name": "Princess Tower", "price_per_m2": 21000, "deals": 210},
        ],
        "risks": ["Off-plan delivery риск", "Volatility 12% YoY", "AED/RUB колебания"],
        "signals": ["RERA escrow", "Top-3 liquidity Dubai", "Emaar — проверенный застройщик"],
    }
    out = generate_pdf_report("area", sample, lang="ru", output_dir="./_test")
    print(f"OK -> {out}")
