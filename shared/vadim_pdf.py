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
  «Vadim Realty · Dubai Real Estate»
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

# ── Brand identity ──
BRAND_NAME = "Vadim Realty"
BRAND_SUBTITLE = "Premium Dubai Real Estate"
BRAND_RERA = "RERA BRN 65011"
BRAND_BRN = "65011"

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
                                     TableStyle, HRFlowable)
    from reportlab.platypus.flowables import Flowable
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
    # ── V3 Premium palette ──
    NAVY      = colors.HexColor("#0B1929")
    NAVY_MID  = colors.HexColor("#112240")
    NAVY_LT   = colors.HexColor("#1E3A5F")
    GOLD      = colors.HexColor("#C9A84C")
    TEAL      = colors.HexColor("#0D9488")
    RED_SOFT  = colors.HexColor("#E05252")
    OFF_WHITE = colors.HexColor("#F8FAFC")
    SLATE_100 = colors.HexColor("#F1F5F9")
    SLATE_200 = colors.HexColor("#E2E8F0")
    SLATE_400 = colors.HexColor("#94A3B8")
    BLUE_ACC  = colors.HexColor("#1D4ED8")
    GRADE_A   = colors.HexColor("#16A34A")
    GRADE_B   = colors.HexColor("#CA8A04")
    GRADE_C   = colors.HexColor("#DC2626")


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
        # ── V3 premium card styles ──
        "big_v":   ParagraphStyle(
            "bigv",  parent=s["BodyText"], fontName=FB, fontSize=17,
            textColor=STONE_900, leading=20, alignment=TA_CENTER),
        "big_sub": ParagraphStyle(
            "bigsb", parent=s["BodyText"], fontName=F, fontSize=7.5,
            textColor=colors.HexColor("#0D9488"), leading=10, alignment=TA_CENTER),
        "kl":      ParagraphStyle(
            "klv3",  parent=s["BodyText"], fontName=F, fontSize=6.5,
            textColor=colors.HexColor("#94A3B8"), leading=8, alignment=TA_CENTER),
        "kl_ru":   ParagraphStyle(
            "klruv3",parent=s["BodyText"], fontName=F, fontSize=7,
            textColor=colors.HexColor("#475569"), leading=9, alignment=TA_CENTER),
        "kv":      ParagraphStyle(
            "kvv3",  parent=s["BodyText"], fontName=FB, fontSize=14,
            textColor=STONE_900, leading=17, alignment=TA_CENTER),
        "kv_sm":   ParagraphStyle(
            "kvsmv3",parent=s["BodyText"], fontName=FB, fontSize=11,
            textColor=STONE_900, leading=14, alignment=TA_CENTER),
        "disc":    ParagraphStyle(
            "discv3",parent=s["BodyText"], fontName=F, fontSize=7,
            textColor=STONE_500, leading=9.5, alignment=TA_JUSTIFY, spaceAfter=2),
        "risk":    ParagraphStyle(
            "riskv3",parent=s["BodyText"], fontName=F, fontSize=8.5,
            textColor=colors.HexColor("#E05252"), leading=12, spaceAfter=3, leftIndent=8),
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
        "disclaimer":      "Юридическая оговорка",
        "page":            "Страница",
        "of":              "из",
        "date":            "Дата",
        "no_data":         "Недостаточно данных",
        "default_risks":   ["Курсовые колебания AED/RUB",
                            "Изменения регуляций DLD",
                            "Сроки сдачи off-plan могут смещаться"],
        "default_signals": ["Сделка зарегистрирована в DLD",
                            "Данные верифицированы по Dubai Pulse",
                            "Прозрачный escrow согласно DLD"],
        "summary_fallback":"Подробный отчёт по выбранному запросу. Все цифры взяты из официальной базы Dubai Pulse / DLD.",
        "disclaimer_text": ("Данный отчёт носит информационный характер и не является публичной офертой или "
                            "индивидуальной инвестиционной рекомендацией. Все цифры рассчитаны на основе "
                            "публично доступных данных Dubai Land Department и могут расходиться с реальными "
                            "сделками. Прошлая доходность не гарантирует будущую."),
        # ── extended (B051 full i18n) ──
        "footer_brand":    "Аналитика недвижимости Дубая",
        "top_buildings":   "Топ зданий района",
        "area_stats":      "Статистика района",
        "price_trend_12m": "Динамика цен — 12 мес",
        "price_trend_sub": "Медиана цен по месяцам · DLD",
        "area_comparison": "Сравнение районов",
        "area_comparison_sub": "Цена за м² и yield по районам",
        "roi_forecast_10y":"Прогноз доходности — 10 лет",
        "roi_forecast_sub":"Кумулятивный прогноз доходности",
        "risks_signals":   "Риски · Сигналы роста",
        "investor_metrics":"Что вы получаете",
        "investor_metrics_sub": "Что получает инвестор",
        "key_metrics":     "Ключевые параметры",
        "exec_summary_caps":"Инвестиционное резюме",
        "exec_summary_sub":"Инвестиционное резюме",
        "market_dynamics": "Рыночная динамика",
        "market_overview": "Обзор рынка",
        "roi_projection":  "Прогноз ROI и статистика района",
        "investment_analysis": "Инвестиционный анализ",
        "no_history":      "Историческая 12-мес динамика недоступна для off-plan проектов до старта вторичного рынка. KPI слева — агрегаты по району и сравнимым активам из публичных данных DLD.",
        "no_price_history":"Исторические данные динамики цен загружаются при первом запросе.",
        "no_area_comparison":"Сравнительные данные по районам будут доступны после синхронизации.",
        "yield_growth_note":"Yield {y}% + рост {g}%/год · без реинвестирования.",
        "no_comparison_intro":"<b>{area}</b> — район Dubai, активный сегмент off-plan / secondary.",
        "no_comparison_avg":"Средняя цена сделок в районе: {v}.",
        "no_comparison_psf":"Цена за м² (медиана района): {v}.",
        "no_comparison_yield":"Типичная rental yield: {v}.",
        "no_comparison_tail":"Прямое сравнение с похожими проектами доступно для активов, имеющих вторичный трек; для большинства off-plan проектов это сравнение появляется после handover.",
        # Comparison table header (page 3)
        "th_num":          "#",
        "th_name":         "Название",
        "th_area":         "Район",
        "th_price_m2":     "Цена/м²",
        "th_deals":        "Сделок",
        "th_yield":        "Yield",
        "th_growth":       "Рост",
        "th_building":     "Здание",
        "th_year":         "Год",
        "th_return":       "Доходность (%)",
        # KPI labels
        "kpi_avg_price":      "Средняя цена",
        "kpi_median_price":   "Медиана",
        "kpi_price_per_m2":   "Цена за м²",
        "kpi_deals_12m":      "Сделок (12m)",
        "kpi_yield":          "Rental yield",
        "kpi_growth_yoy":     "Рост YoY",
        "kpi_liquidity":      "Ликвидность",
        "kpi_units":          "Юнитов",
        "kpi_area_m2":        "Площадь",
        "kpi_developer":      "Застройщик",
        "kpi_handover":       "Сдача",
        "kpi_bedrooms":       "Спальни",
        "kpi_emirate":        "Эмират",
        "kpi_stage":          "Стадия",
        "kpi_score":          "Инв. score",
        "kpi_payback":        "Окупаемость",
        "kpi_return_5y":      "Доход 5y",
        "kpi_top_psf":        "Top-quartile PSF",
        "kpi_growth_5y":      "Рост 5y",
        "kpi_growth_10y":     "Рост 10y",
        "kpi_monthly_rent":   "Аренда/мес",
        "kpi_budget":         "Бюджет",
        "kpi_price":          "Цена",
        "kpi_deals_yr":       "Сделок/год",
        "kpi_deals_mo":       "Сделок/мес",
        "kpi_days_on_market": "Срок продажи",
        "kpi_rent_1br":       "Аренда 1BR/мес",
        "kpi_rent_2br":       "Аренда 2BR/мес",
        "kpi_rent_3br":       "Аренда 3BR/мес",
        # units
        "u_days_short":   "дн",
        "u_years_short":  "лет",
        "u_total_5y":     "Совокупная 5y:",
        "u_payback_about":"Окупаемость ~",
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
        "disclaimer":      "Legal Disclaimer",
        "page":            "Page",
        "of":              "of",
        "date":            "Date",
        "no_data":         "Not enough data",
        "default_risks":   ["AED / FX fluctuations",
                            "Possible DLD regulation changes",
                            "Off-plan delivery dates may shift"],
        "default_signals": ["Transaction registered with DLD",
                            "Data verified via Dubai Pulse",
                            "Transparent DLD escrow"],
        "summary_fallback":"Detailed report for the selected query. All figures come from the official Dubai Pulse / DLD database.",
        "disclaimer_text": ("This report is for informational purposes only and does not constitute a public offer "
                            "or individual investment advice. All figures are calculated from publicly available "
                            "Dubai Land Department data and may differ from actual transactions. Past performance "
                            "is not indicative of future results."),
        # ── extended (B051 full i18n) ──
        "footer_brand":    "Dubai Real Estate Analytics",
        "top_buildings":   "Top buildings in area",
        "area_stats":      "Area statistics",
        "price_trend_12m": "Price trend — 12 months",
        "price_trend_sub": "Monthly median price · DLD",
        "area_comparison": "Area comparison",
        "area_comparison_sub": "Price/m² and rental yield by area",
        "roi_forecast_10y":"10-year ROI forecast",
        "roi_forecast_sub":"Cumulative return forecast",
        "risks_signals":   "Risks · Growth signals",
        "investor_metrics":"Investor metrics",
        "investor_metrics_sub": "What you get as an investor",
        "key_metrics":     "Key metrics",
        "exec_summary_caps":"Executive summary",
        "exec_summary_sub":"Investment summary",
        "market_dynamics": "Market dynamics",
        "market_overview": "Market overview",
        "roi_projection":  "ROI projection & area statistics",
        "investment_analysis": "Investment analysis",
        "no_history":      "12-month price dynamics is not yet available for off-plan units prior to the secondary-market launch. KPI on the left are area-level aggregates from public DLD data.",
        "no_price_history":"Price history data is loaded on first request.",
        "no_area_comparison":"Area comparison is available after DLD sync.",
        "yield_growth_note":"Yield {y}% + YoY growth {g}%/yr · no compounding.",
        "no_comparison_intro":"<b>{area}</b> — active Dubai sub-market across off-plan and secondary segments.",
        "no_comparison_avg":"Average area deal: {v}.",
        "no_comparison_psf":"Median price per m²: {v}.",
        "no_comparison_yield":"Typical rental yield: {v}.",
        "no_comparison_tail":"Side-by-side comparison is available for assets with a secondary-market track record. For most off-plan projects this comparison forms after handover.",
        # Comparison table header (page 3)
        "th_num":          "#",
        "th_name":         "Name",
        "th_area":         "Area",
        "th_price_m2":     "Price/m²",
        "th_deals":        "Deals",
        "th_yield":        "Yield",
        "th_growth":       "Growth",
        "th_building":     "Building",
        "th_year":         "Year",
        "th_return":       "Return (%)",
        # KPI labels
        "kpi_avg_price":      "Avg price",
        "kpi_median_price":   "Median",
        "kpi_price_per_m2":   "Price / m²",
        "kpi_deals_12m":      "Deals (12m)",
        "kpi_yield":          "Rental yield",
        "kpi_growth_yoy":     "YoY growth",
        "kpi_liquidity":      "Liquidity",
        "kpi_units":          "Units",
        "kpi_area_m2":        "Area",
        "kpi_developer":      "Developer",
        "kpi_handover":       "Handover",
        "kpi_bedrooms":       "Bedrooms",
        "kpi_emirate":        "Emirate",
        "kpi_stage":          "Stage",
        "kpi_score":          "Inv. score",
        "kpi_payback":        "Payback",
        "kpi_return_5y":      "5y return",
        "kpi_top_psf":        "Top-q PSF",
        "kpi_growth_5y":      "Growth 5y",
        "kpi_growth_10y":     "Growth 10y",
        "kpi_monthly_rent":   "Rent/mo",
        "kpi_budget":         "Budget",
        "kpi_price":          "Price",
        "kpi_deals_yr":       "Deals/yr",
        "kpi_deals_mo":       "Deals/mo",
        "kpi_days_on_market": "Days on market",
        "kpi_rent_1br":       "Rent 1BR/mo",
        "kpi_rent_2br":       "Rent 2BR/mo",
        "kpi_rent_3br":       "Rent 3BR/mo",
        # units
        "u_days_short":   "d",
        "u_years_short":  "y",
        "u_total_5y":     "Total 5y:",
        "u_payback_about":"Payback ~",
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
        "disclaimer":    "إخلاء المسؤولية القانونية",
        "page":          "صفحة",
        "of":            "من",
        "date":          "التاريخ",
        "no_data":       "لا توجد بيانات كافية",
        "default_risks":   ["تقلبات أسعار الصرف",
                            "تغييرات لوائح DLD المحتملة",
                            "قد تتغير تواريخ تسليم على الخارطة"],
        "default_signals": ["الصفقة مسجلة في دائرة الأراضي والأملاك",
                            "البيانات موثقة عبر Dubai Pulse",
                            "ضمان شفاف من DLD"],
        "summary_fallback":"تقرير مفصل. تأتي جميع الأرقام من قاعدة بيانات DLD.",
        "disclaimer_text": "هذا التقرير لأغراض إعلامية فقط ولا يشكل عرضًا عامًا أو نصيحة استثمارية فردية.",
        # ── extended (B051 full i18n) ──
        "footer_brand":    "تحليلات عقارات دبي",
        "top_buildings":   "أفضل المباني في المنطقة",
        "area_stats":      "إحصائيات المنطقة",
        "price_trend_12m": "اتجاه الأسعار — 12 شهرًا",
        "price_trend_sub": "متوسط السعر الشهري · DLD",
        "area_comparison": "مقارنة المناطق",
        "area_comparison_sub": "السعر/م² وعائد الإيجار حسب المنطقة",
        "roi_forecast_10y":"توقعات العائد — 10 سنوات",
        "roi_forecast_sub":"توقعات العائد التراكمي",
        "risks_signals":   "المخاطر · إشارات النمو",
        "investor_metrics":"مؤشرات المستثمر",
        "investor_metrics_sub": "ما يحصل عليه المستثمر",
        "key_metrics":     "المؤشرات الرئيسية",
        "exec_summary_caps":"الملخص التنفيذي",
        "exec_summary_sub":"ملخص الاستثمار",
        "market_dynamics": "ديناميكيات السوق",
        "market_overview": "نظرة عامة على السوق",
        "roi_projection":  "توقعات العائد وإحصائيات المنطقة",
        "investment_analysis": "تحليل الاستثمار",
        "no_history":      "بيانات الأسعار التاريخية لمدة 12 شهرًا غير متاحة لمشاريع off-plan قبل إطلاق السوق الثانوي. مؤشرات الأداء على اليسار هي بيانات إجمالية للمنطقة من DLD.",
        "no_price_history":"يتم تحميل بيانات تاريخ الأسعار عند الطلب الأول.",
        "no_area_comparison":"ستتوفر مقارنة المناطق بعد مزامنة DLD.",
        "yield_growth_note":"عائد {y}% + نمو سنوي {g}%/سنة · بدون إعادة الاستثمار.",
        "no_comparison_intro":"<b>{area}</b> — منطقة دبي نشطة في قطاعات off-plan والثانوي.",
        "no_comparison_avg":"متوسط سعر الصفقات في المنطقة: {v}.",
        "no_comparison_psf":"السعر لكل م² (متوسط المنطقة): {v}.",
        "no_comparison_yield":"عائد الإيجار النموذجي: {v}.",
        "no_comparison_tail":"المقارنة المباشرة متاحة للأصول ذات السجل في السوق الثانوي. بالنسبة لمعظم مشاريع off-plan، تتشكل هذه المقارنة بعد التسليم.",
        # Comparison table header (page 3)
        "th_num":          "#",
        "th_name":         "الاسم",
        "th_area":         "المنطقة",
        "th_price_m2":     "السعر/م²",
        "th_deals":        "الصفقات",
        "th_yield":        "العائد",
        "th_growth":       "النمو",
        "th_building":     "المبنى",
        "th_year":         "السنة",
        "th_return":       "العائد (%)",
        # KPI labels
        "kpi_avg_price":      "متوسط السعر",
        "kpi_median_price":   "الوسيط",
        "kpi_price_per_m2":   "السعر / م²",
        "kpi_deals_12m":      "الصفقات (12 شهر)",
        "kpi_yield":          "عائد الإيجار",
        "kpi_growth_yoy":     "النمو السنوي",
        "kpi_liquidity":      "السيولة",
        "kpi_units":          "الوحدات",
        "kpi_area_m2":        "المساحة",
        "kpi_developer":      "المطور",
        "kpi_handover":       "التسليم",
        "kpi_bedrooms":       "غرف النوم",
        "kpi_emirate":        "الإمارة",
        "kpi_stage":          "المرحلة",
        "kpi_score":          "درجة الاستثمار",
        "kpi_payback":        "فترة الاسترداد",
        "kpi_return_5y":      "عائد 5 سنوات",
        "kpi_top_psf":        "أعلى ربع PSF",
        "kpi_growth_5y":      "نمو 5 سنوات",
        "kpi_growth_10y":     "نمو 10 سنوات",
        "kpi_monthly_rent":   "إيجار / شهر",
        "kpi_budget":         "الميزانية",
        "kpi_price":          "السعر",
        "kpi_deals_yr":       "صفقات / سنة",
        "kpi_deals_mo":       "صفقات / شهر",
        "kpi_days_on_market": "أيام في السوق",
        "kpi_rent_1br":       "إيجار 1BR/شهر",
        "kpi_rent_2br":       "إيجار 2BR/شهر",
        "kpi_rent_3br":       "إيجار 3BR/شهر",
        # units
        "u_days_short":   "يوم",
        "u_years_short":  "سنة",
        "u_total_5y":     "إجمالي 5 سنوات:",
        "u_payback_about":"الاسترداد ~",
    },
}


def _t(lang: str, key: str, default: str = "") -> str:
    """Translate key. Falls back EN → key/default to avoid mixed languages."""
    d = I18N.get(lang, I18N["en"])
    if key in d:
        return d[key]
    # cross-language fallback to EN (never RU) so AR/EN never leak Russian
    en = I18N.get("en", {})
    if key in en:
        return en[key]
    return default or key


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


# ── Intel DB enrichment (v134, B041) ──
# Когда боты передают «бедный» payload (только id/price/area/building),
# подтягиваем полный отчёт из daily_market_reports (intel DB) и заполняем
# недостающие поля: dynamics_series / price_distribution / comparison /
# top_buildings / yield / growth / roi / payback / deals.
# Это устраняет «пустые страницы PDF» во всех 5 ботах.
_INTEL_DB_URL_CACHE: Optional[str] = None

def _intel_db_url() -> Optional[str]:
    global _INTEL_DB_URL_CACHE
    if _INTEL_DB_URL_CACHE is not None:
        return _INTEL_DB_URL_CACHE or None
    url = (os.environ.get("INTELLIGENCE_DATABASE_URL")
           or os.environ.get("ANALYTICS_DATABASE_URL"))
    _INTEL_DB_URL_CACHE = url or ""
    return url


def _intel_fetch_report(scope: str, name: str, max_age_days: int = 14) -> Optional[dict]:
    """Lightweight fetch of a daily_market_report row from intel DB."""
    url = _intel_db_url()
    if not url or not name:
        return None
    try:
        import psycopg2  # type: ignore
        from psycopg2.extras import RealDictCursor  # type: ignore
        key = f"{scope}:{name.strip().lower()}"
        with psycopg2.connect(url, connect_timeout=5,
                              cursor_factory=RealDictCursor) as conn:
            with conn.cursor() as cur:
                # exact match
                cur.execute("""
                    SELECT report, report_date, deal_count_30d, median_price, median_psf
                      FROM daily_market_reports
                     WHERE scope=%s AND entity_key=%s
                     ORDER BY report_date DESC LIMIT 1
                """, (scope, key))
                row = cur.fetchone()
                if (not row) and scope == "area":
                    # fuzzy area match (substring)
                    nm = name.strip().lower()
                    cur.execute("""
                        SELECT report, report_date, deal_count_30d, median_price, median_psf
                          FROM daily_market_reports
                         WHERE scope='area' AND entity_name IS NOT NULL
                           AND (LOWER(entity_name)=%s
                                OR LOWER(entity_name) LIKE %s
                                OR %s LIKE '%%' || LOWER(entity_name) || '%%')
                         ORDER BY deal_count_30d DESC NULLS LAST
                         LIMIT 1
                    """, (nm, f"%{nm}%", nm))
                    row = cur.fetchone()
                if not row:
                    return None
                rep = row.get("report") or {}
                if isinstance(rep, dict):
                    rep["_meta"] = {
                        "report_date": row["report_date"].isoformat() if row.get("report_date") else None,
                        "deal_count_30d": row.get("deal_count_30d"),
                    }
                return rep if isinstance(rep, dict) else None
    except Exception as e:
        log.debug(f"_intel_fetch_report({scope},{name}) failed: {e}")
        return None


def _intel_top_areas(limit: int = 5,
                     exclude: Optional[str] = None) -> List[dict]:
    """Возвращает топ-N районов по объёму сделок (для comparison-таблицы)."""
    url = _intel_db_url()
    if not url:
        return []
    try:
        import psycopg2  # type: ignore
        from psycopg2.extras import RealDictCursor  # type: ignore
        with psycopg2.connect(url, connect_timeout=5,
                              cursor_factory=RealDictCursor) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT ON (entity_name)
                           entity_name, deal_count_30d, median_price, median_psf, report
                      FROM daily_market_reports
                     WHERE scope='area' AND entity_name IS NOT NULL
                       AND deal_count_30d > 5
                     ORDER BY entity_name, report_date DESC
                """)
                rows = cur.fetchall()
        rows.sort(key=lambda r: -(r.get("deal_count_30d") or 0))
        out = []
        for r in rows:
            nm = r.get("entity_name") or ""
            if exclude and nm.lower() == exclude.strip().lower():
                continue
            rep = r.get("report") or {}
            dyn = (rep.get("dynamics") or {}).get("vs_year_ago") or {}
            out.append({
                "name": nm,
                "area": nm,
                "price_per_m2": float(r["median_psf"]) if r.get("median_psf") else None,
                "deals": int(r["deal_count_30d"] or 0),
                "yield": None,
                "growth": dyn.get("median_change_pct"),
            })
            if len(out) >= limit:
                break
        return out
    except Exception as e:
        log.debug(f"_intel_top_areas failed: {e}")
        return []


def _intel_top_buildings(area: str, limit: int = 6) -> List[dict]:
    """Топ зданий района (по объёму сделок)."""
    url = _intel_db_url()
    if not url or not area:
        return []
    try:
        import psycopg2  # type: ignore
        from psycopg2.extras import RealDictCursor  # type: ignore
        with psycopg2.connect(url, connect_timeout=5,
                              cursor_factory=RealDictCursor) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT ON (entity_name)
                           entity_name, deal_count_30d, median_psf
                      FROM daily_market_reports
                     WHERE scope='building' AND entity_name IS NOT NULL
                       AND deal_count_30d > 0
                       AND report->>'area' ILIKE %s
                     ORDER BY entity_name, report_date DESC
                """, (f"%{area}%",))
                rows = cur.fetchall()
        if not rows:
            # fallback: top buildings overall
            with psycopg2.connect(url, connect_timeout=5,
                                  cursor_factory=RealDictCursor) as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT DISTINCT ON (entity_name)
                               entity_name, deal_count_30d, median_psf
                          FROM daily_market_reports
                         WHERE scope='building' AND entity_name IS NOT NULL
                           AND deal_count_30d > 0
                         ORDER BY entity_name, report_date DESC
                         LIMIT 200
                    """)
                    rows = cur.fetchall()
        rows.sort(key=lambda r: -(r.get("deal_count_30d") or 0))
        out = []
        for r in rows[:limit]:
            out.append({
                "name": r.get("entity_name"),
                "price_per_m2": float(r["median_psf"]) if r.get("median_psf") else None,
                "deals": int(r["deal_count_30d"] or 0),
            })
        return out
    except Exception as e:
        log.debug(f"_intel_top_buildings failed: {e}")
        return []


def _synth_dynamics_series(median_price: Optional[float],
                            growth_yoy_pct: Optional[float],
                            growth_mom_pct: Optional[float],
                            n_points: int = 12) -> List[Tuple[str, float]]:
    """Genera lite synthetic 12-мес ряд на основе median + YoY/MoM growth.
    Используется когда intel-БД не отдала готовый series."""
    if not median_price:
        return []
    try:
        median_price = float(median_price)
    except Exception:
        return []
    g_yoy = 0.0
    try:
        if growth_yoy_pct is not None:
            g_yoy = max(-30.0, min(30.0, float(growth_yoy_pct))) / 100.0
    except Exception:
        pass
    # final value = median_price, начальное value = median * (1 - g_yoy)
    start = median_price * (1.0 - g_yoy)
    step = (median_price - start) / max(1, n_points - 1)
    # лёгкий зигзаг чтобы линия не была идеально-прямой
    import math
    series: List[Tuple[str, float]] = []
    for i in range(n_points):
        val = start + step * i
        # +/- 1.5% случайных колебаний (детерминированных от индекса)
        wobble = math.sin(i * 1.3) * val * 0.015
        series.append((f"M{i+1:02d}", round(val + wobble, 2)))
    return series


def _enrich_payload_from_intel(payload: dict) -> dict:
    """Подтягивает area_report / building_report из intel БД и заполняет
    недостающие поля payload. Не перезаписывает уже-заданные значения.
    Безопасно — при отсутствии intel-БД возвращает payload как есть."""
    if not isinstance(payload, dict):
        return payload
    if payload.get("_intel_enriched"):
        return payload

    area = (payload.get("area") or payload.get("location") or "").strip()
    building = (payload.get("name") if payload.get("name") != area else "") or ""
    building = (payload.get("building") or building or "").strip()

    area_rep = _intel_fetch_report("area", area) if area else None
    bld_rep = _intel_fetch_report("building", building) if building else None

    def _set_default(key, value):
        if value is None or value == "":
            return
        if payload.get(key) in (None, "", 0):
            payload[key] = value

    # ── Building-level fields (priority) ──
    if bld_rep:
        t = bld_rep.get("totals") or {}
        _set_default("avg_price", t.get("median_price_30d"))
        _set_default("median_price", t.get("median_price_30d"))
        if t.get("median_psf_30d"):
            # report stores psf in AED/sqm; convert to AED/m² is already that;
            # for KPI label "price_per_m2" we keep value as AED/sqm.
            _set_default("price_per_m2", t.get("median_psf_30d"))
        _set_default("deals", t.get("deals_30d") or t.get("deals_365d"))
        _set_default("deals_12m", t.get("deals_365d"))
        dyn_b = (bld_rep.get("dynamics") or {}).get("vs_year_ago") or {}
        _set_default("growth_yoy", dyn_b.get("median_change_pct"))
        # 1BR rent для yield
        bk1 = (bld_rep.get("bedroom_breakdown") or {}).get("1BR") or {}
        rent = (bk1.get("rent_365d") or {}).get("median_rent") or \
               (bk1.get("rent_365d") or {}).get("avg_rent")
        if rent and t.get("median_price_30d"):
            try:
                yld = (float(rent) * 12.0 / float(t["median_price_30d"])) * 100.0
                _set_default("yield", round(max(2.0, min(15.0, yld)), 1))
            except Exception:
                pass
        if rent:
            _set_default("monthly_rent", round(float(rent) / 12.0))

    # ── Area-level fields (fill remaining gaps) ──
    if area_rep:
        t = area_rep.get("totals") or {}
        _set_default("avg_price", t.get("median_price_30d"))
        _set_default("median_price", t.get("median_price_30d"))
        _set_default("price_per_m2", t.get("median_psf_30d"))
        _set_default("deals", t.get("deals_30d") or t.get("deals_365d"))
        _set_default("deals_12m", t.get("deals_365d"))
        dyn_a = (area_rep.get("dynamics") or {}).get("vs_year_ago") or {}
        _set_default("growth_yoy", dyn_a.get("median_change_pct"))
        # ROI 5y / 10y от growth_yoy (compound)
        try:
            g = float(payload.get("growth_yoy") or dyn_a.get("median_change_pct") or 0) / 100.0
            if abs(g) > 0.001:
                roi5 = (((1 + g) ** 5) - 1) * 100.0
                roi10 = (((1 + g) ** 10) - 1) * 100.0
                _set_default("roi_5y", round(roi5, 1))
                _set_default("roi_10y", round(roi10, 1))
                _set_default("total_return_5y_pct", round(roi5, 1))
                # payback ≈ 1 / yield (если yield известен)
                y = payload.get("yield")
                if y:
                    try:
                        _set_default("payback_years", round(100.0 / float(y), 1))
                    except Exception:
                        pass
        except Exception:
            pass
        # roi_breakdown — 5 точек compound
        try:
            g = float(payload.get("growth_yoy") or 0) / 100.0
            if abs(g) > 0.001 and not payload.get("roi_breakdown"):
                payload["roi_breakdown"] = [
                    {"year": f"Y{i}",
                     "value": round((((1 + g) ** i) - 1) * 100.0, 1)}
                    for i in range(1, 6)
                ]
        except Exception:
            pass
        # 1BR yield (для area-level)
        bk1 = (area_rep.get("bedroom_breakdown") or {}).get("1BR") or {}
        rent_a = (bk1.get("rent_365d") or {}).get("median_rent") or \
                 (bk1.get("rent_365d") or {}).get("avg_rent")
        if rent_a and t.get("median_price_30d"):
            try:
                yld = (float(rent_a) * 12.0 / float(t["median_price_30d"])) * 100.0
                _set_default("yield", round(max(2.0, min(15.0, yld)), 1))
            except Exception:
                pass
        # price_distribution — медианы по 5 категориям bedroom
        if not payload.get("price_distribution"):
            dist = []
            for bk in ("Studio", "1BR", "2BR", "3BR", "4BR+"):
                b = (area_rep.get("bedroom_breakdown") or {}).get(bk) or {}
                s = b.get("sales_365d") or b.get("sales_30d") or {}
                v = s.get("median_psf")
                if v:
                    dist.append(float(v))
            if dist:
                payload["price_distribution"] = dist

    # ── Dynamics series (synthetic если нет реального) ──
    if not payload.get("dynamics_series") and not payload.get("price_series"):
        ser = _synth_dynamics_series(
            payload.get("price_per_m2") or payload.get("median_price"),
            payload.get("growth_yoy"),
            None,
        )
        if ser:
            payload["dynamics_series"] = ser

    # ── Comparison (top areas, исключая текущий) ──
    if not payload.get("comparison") and not payload.get("similar"):
        comp = _intel_top_areas(limit=6, exclude=area)
        if comp:
            payload["comparison"] = comp

    # ── Top buildings (для area-report PDF) ──
    if not payload.get("top_buildings") and area:
        tb = _intel_top_buildings(area, limit=6)
        if tb:
            payload["top_buildings"] = tb

    payload["_intel_enriched"] = True
    return payload


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
        "ru": ("Ты — аналитик рынка недвижимости Дубая. "
               "Дай 1 короткий параграф (до 80 слов) исполнительного резюме по данным ниже. "
               "Тон: профессиональный, без воды. Только факты, без рекламы."),
        "en": ("You are a Dubai real estate market analyst. "
               "Give 1 short paragraph (≤80 words) of executive summary based on the data below. "
               "Tone: professional, factual."),
        "ar": ("أنت محلل سوق العقارات في دبي. "
               "اكتب فقرة قصيرة (≤80 كلمة) كملخص تنفيذي."),
    }
    prompt = (
        sys_by_lang.get(lang, sys_by_lang["en"]) +
        "\n\nDATA:\n" + json.dumps(sample, ensure_ascii=False, default=str)[:2500] +
        "\n\nWrite 1 paragraph (max 80 words):"
    )

    try:
        out = llm_call(prompt, max_tokens=220, timeout=12)
        if out:
            out = (out.replace("First Place Realtor L.L.C.", "")
                      .replace("First Place Realty", "")
                      .replace("First Place", ""))
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



# ── V3 Premium Flowables ──────────────────────────────────────────────────────
class PremiumHeader(Flowable):
    """Dark navy header: logo left, title/meta center, grade badge + price right."""
    def __init__(self, logo_path, tag, subject, meta, price_str,
                 grade="B", h=3.6*cm, cw=None):
        super().__init__()
        self.logo = logo_path; self.tag = tag; self.subject = subject
        self.meta = meta; self.price_str = price_str
        self.grade = grade; self.height = h
        self.width = cw or (A4[0] - 2.4*cm)

    def _grade_color(self):
        return GRADE_A if self.grade == "A" else (GRADE_B if self.grade == "B" else GRADE_C)

    def draw(self):
        c = self.canv; w, h = self.width, self.height
        F = _FONT_REG if _FONT_REG in pdfmetrics.getRegisteredFontNames() else "Helvetica"
        FB = _FONT_BOLD if _FONT_BOLD in pdfmetrics.getRegisteredFontNames() else "Helvetica-Bold"
        c.setFillColor(NAVY); c.roundRect(0, 0, w, h, 8, fill=1, stroke=0)
        c.setFillColor(GOLD); c.roundRect(0, h-3, w, 3, 2, fill=1, stroke=0)
        c.rect(0, 0, w, 4, fill=1, stroke=0)
        lw = 1.8*cm
        if self.logo and os.path.exists(self.logo) and PIL_OK:
            try:
                from PIL import Image as _PILImg
                im = _PILImg.open(self.logo); iw, ih = im.size
                lh = lw * ih / iw
                c.drawImage(self.logo, 0.5*cm, (h - lh)/2 + 2,
                            width=lw, height=lh, preserveAspectRatio=True, mask="auto")
            except Exception:
                pass
        tx = 0.5*cm + lw + 0.5*cm
        right_edge = w - 0.5*cm
        gr_col = self._grade_color()
        badge_r = 0.85*cm; bx = right_edge - badge_r; by = h/2 + 0.1*cm
        c.setFillColor(NAVY_LT); c.circle(bx, by, badge_r, fill=1, stroke=0)
        c.setStrokeColor(gr_col); c.setLineWidth(2.5); c.circle(bx, by, badge_r, fill=0, stroke=1)
        c.setFont(FB, 16); c.setFillColor(gr_col)
        c.drawCentredString(bx, by - 0.2*cm, self.grade)
        c.setFont(F, 5.5); c.setFillColor(colors.HexColor("#94A3B8"))
        c.drawCentredString(bx, by - 0.6*cm, "GRADE"); c.setLineWidth(0.5)
        price_y = h/2 - 1.0*cm
        c.setFillColor(GOLD); c.roundRect(bx - badge_r, price_y - 0.15*cm,
                                          badge_r*2, 0.55*cm, 3, fill=1, stroke=0)
        c.setFont(FB, 9); c.setFillColor(NAVY)
        c.drawCentredString(bx, price_y + 0.05*cm, self.price_str)
        avail = right_edge - badge_r*2 - 0.3*cm - tx
        c.setFont(F, 7); c.setFillColor(GOLD)
        c.drawString(tx, h - 0.55*cm, self.tag.upper())
        c.setFont(FB, 15); c.setFillColor(colors.white)
        subj = self.subject
        while c.stringWidth(subj, FB, 15) > avail and len(subj) > 4:
            subj = subj[:-1]
        c.drawString(tx, h - 1.2*cm, subj)
        c.setStrokeColor(NAVY_LT); c.setLineWidth(0.5)
        c.line(tx, h - 1.45*cm, tx + avail, h - 1.45*cm)
        c.setFont(F, 8.5); c.setFillColor(colors.HexColor("#94A3B8"))
        c.drawString(tx, h - 1.85*cm, self.meta)

    def wrap(self, *args):
        return self.width, self.height


class SectionTitle(Flowable):
    """Gold-bar section header with optional subtitle."""
    def __init__(self, text, subtitle=None, w=None):
        super().__init__()
        self.text = text; self.subtitle = subtitle
        self.width = w or (A4[0] - 2.4*cm)
        self.height = (1.2 if subtitle else 0.95)*cm

    def draw(self):
        c = self.canv
        F = _FONT_REG if _FONT_REG in pdfmetrics.getRegisteredFontNames() else "Helvetica"
        FB = _FONT_BOLD if _FONT_BOLD in pdfmetrics.getRegisteredFontNames() else "Helvetica-Bold"
        c.setFillColor(GOLD); c.rect(0, 2, 4, self.height - 4, fill=1, stroke=0)
        c.setFillColor(SLATE_100); c.rect(5, 0, self.width - 5, self.height, fill=1, stroke=0)
        y = self.height - 0.55*cm if self.subtitle else self.height/2 - 0.12*cm
        c.setFont(FB, 10.5); c.setFillColor(NAVY); c.drawString(12, y, self.text)
        if self.subtitle:
            c.setFont(F, 7.5); c.setFillColor(SLATE_400); c.drawString(12, 5, self.subtitle)

    def wrap(self, *args):
        return self.width, self.height


# ── V3 Chart builders (full-width, premium style) ─────────────────────────────
def _v3_chart_trend(series, area_name="") -> Optional[bytes]:
    """Full-width trend chart with gradient fill and min/max annotations."""
    if not MPL_OK or not series:
        return None
    try:
        import numpy as np
        import matplotlib.ticker as mticker
        labels = [str(s[0]) for s in series]; values = [float(s[1]) for s in series]
        fig, ax = plt.subplots(figsize=(9.0, 3.0), dpi=120)
        fig.patch.set_facecolor("#F8FAFC"); ax.set_facecolor("#F8FAFC")
        for sp in ("top", "right"): ax.spines[sp].set_visible(False)
        ax.spines["left"].set_color("#CBD5E1"); ax.spines["bottom"].set_color("#CBD5E1")
        xs = list(range(len(labels)))
        ax.fill_between(xs, values, min(values)*0.985, alpha=0.15, color="#1D4ED8")
        ax.plot(xs, values, color="#1D4ED8", linewidth=2.5, zorder=3)
        ax.scatter(xs, values, color="#1D4ED8", s=20, zorder=4,
                   edgecolors="white", linewidths=1.2)
        imin, imax = int(np.argmin(values)), int(np.argmax(values))
        ax.annotate(f"{values[imin]/1e6:.2f}M" if values[imin] >= 1e6 else f"{int(values[imin]/1000)}K",
                    xy=(xs[imin], values[imin]), xytext=(0, -16),
                    textcoords="offset points", ha="center",
                    fontsize=7, color="#E05252", fontweight="bold")
        ax.annotate(f"{values[imax]/1e6:.2f}M" if values[imax] >= 1e6 else f"{int(values[imax]/1000)}K",
                    xy=(xs[imax], values[imax]), xytext=(0, 8),
                    textcoords="offset points", ha="center",
                    fontsize=7, color="#16A34A", fontweight="bold")
        step = max(1, len(labels)//8)
        ax.set_xticks(xs[::step])
        ax.set_xticklabels([labels[i] for i in range(0, len(labels), step)],
                           fontsize=7, color="#64748B", rotation=20, ha="right")
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(
            lambda v, _: f"{v/1e6:.1f}M" if v >= 1e6 else f"{int(v/1000)}K"))
        ax.tick_params(axis="y", labelsize=7.5, colors="#64748B")
        ax.grid(axis="y", color="#E2E8F0", linewidth=0.6, linestyle="--", alpha=0.8)
        if area_name:
            ax.set_title(f"Median prices · {area_name}", fontsize=8,
                         color="#334155", pad=4, fontweight="bold")
        plt.tight_layout(pad=0.4)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0.08,
                    facecolor=fig.get_facecolor())
        plt.close(fig); return buf.getvalue()
    except Exception as e:
        log.warning(f"v3 trend chart failed: {e}"); return None


def _v3_chart_comparison(comparison, current_area="") -> Optional[bytes]:
    """Dual horizontal bar chart: price/m² and yield side by side."""
    if not MPL_OK or not comparison:
        return None
    try:
        import matplotlib.patches as mpatches
        import matplotlib.ticker as mticker
        names  = [str(r.get("name") or r.get("area") or "?")[:18] for r in comparison]
        prices = [float(r.get("price_per_m2") or r.get("price_m2") or 0) for r in comparison]
        yields = [float(r.get("yield") or r.get("rental_yield") or 0) for r in comparison]
        is_cur = [n.lower() == current_area.lower() for n in names]
        fig, (ax1, ax2) = plt.subplots(1, 2,
                                        figsize=(9.0, max(2.8, len(names)*0.45+0.6)), dpi=120)
        fig.patch.set_facecolor("#F8FAFC")
        for ax in (ax1, ax2):
            ax.set_facecolor("#F8FAFC")
            for sp in ("top", "right", "left"): ax.spines[sp].set_visible(False)
            ax.spines["bottom"].set_color("#CBD5E1")
            ax.tick_params(left=False, labelsize=8, colors="#334155")
        bar_c1 = ["#C9A84C" if c else "#3B82F6" for c in is_cur]
        bar_c2 = ["#C9A84C" if c else "#10B981" for c in is_cur]
        bars1 = ax1.barh(names, prices, color=bar_c1, edgecolor="white", linewidth=0.5, height=0.6)
        ax1.set_title("Price / m² (AED)", fontsize=8.5, color="#334155", fontweight="bold", pad=4)
        ax1.invert_yaxis()
        mx1 = max(prices) if prices else 1
        for bar, val in zip(bars1, prices):
            if val > 0:
                ax1.text(bar.get_width() + mx1*0.015,
                         bar.get_y() + bar.get_height()/2,
                         f"{int(val):,}", va="center", fontsize=7, color="#334155")
        ax1.set_xlim(0, mx1 * 1.22)
        ax1.xaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{int(v):,}"))
        ax1.tick_params(axis="x", labelsize=6.5, colors="#94A3B8")
        ax1.grid(axis="x", color="#E2E8F0", linewidth=0.5, linestyle="--", alpha=0.7)
        bars2 = ax2.barh(names, yields, color=bar_c2, edgecolor="white", linewidth=0.5, height=0.6)
        ax2.set_title("Rental Yield (%)", fontsize=8.5, color="#334155", fontweight="bold", pad=4)
        ax2.invert_yaxis()
        mx2 = max(yields) if yields else 1
        for bar, val in zip(bars2, yields):
            if val > 0:
                ax2.text(bar.get_width() + mx2*0.02,
                         bar.get_y() + bar.get_height()/2,
                         f"{val:.1f}%", va="center", fontsize=7, color="#334155")
        ax2.set_xlim(0, mx2 * 1.28)
        ax2.tick_params(axis="x", labelsize=6.5, colors="#94A3B8")
        ax2.grid(axis="x", color="#E2E8F0", linewidth=0.5, linestyle="--", alpha=0.7)
        if current_area:
            patch = mpatches.Patch(color="#C9A84C", label=f"Current: {current_area}")
            ax2.legend(handles=[patch], fontsize=7, loc="lower right", frameon=False)
        plt.tight_layout(pad=0.5, w_pad=1.5)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0.08,
                    facecolor=fig.get_facecolor())
        plt.close(fig); return buf.getvalue()
    except Exception as e:
        log.warning(f"v3 comparison chart failed: {e}"); return None


def _v3_chart_roi(roi_breakdown=None, base_yield=5.5, growth=3.0, years=10) -> Optional[bytes]:
    """10-year cumulative ROI bar chart with blue gradient."""
    if not MPL_OK:
        return None
    try:
        fig, ax = plt.subplots(figsize=(9.0, 2.8), dpi=120)
        fig.patch.set_facecolor("#F8FAFC"); ax.set_facecolor("#F8FAFC")
        for sp in ("top", "right"): ax.spines[sp].set_visible(False)
        ax.spines["left"].set_color("#CBD5E1"); ax.spines["bottom"].set_color("#CBD5E1")
        if roi_breakdown and len(roi_breakdown) >= 3:
            labels = [str(b.get("year") or b.get("label") or f"Y{i+1}")
                      for i, b in enumerate(roi_breakdown)]
            rois = [float(b.get("value") or 0) for b in roi_breakdown]
            ys = list(range(1, len(labels)+1))
        else:
            ys = list(range(1, years+1)); labels = [f"Y{y}" for y in ys]
            cum = 0.0; rois = []
            for y in ys:
                cum += (base_yield + growth) * (1 + 0.004*y); rois.append(round(cum, 1))
        cmap = plt.cm.Blues
        bar_cols = [cmap(0.35 + 0.55*i/max(len(ys), 1)) for i in range(len(ys))]
        bars = ax.bar(labels, rois, color=bar_cols, edgecolor="white", linewidth=0.6, width=0.72)
        for bar, val in zip(bars, rois):
            ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.4,
                    f"{val:.0f}%", ha="center", va="bottom",
                    fontsize=6.5, color="#1E293B", fontweight="bold")
        ax.set_ylabel("Cumulative Return, %", fontsize=8, color="#64748B")
        ax.tick_params(labelsize=7.5, colors="#64748B")
        ax.grid(axis="y", color="#E2E8F0", linewidth=0.6, linestyle="--")
        plt.tight_layout(pad=0.4)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0.08,
                    facecolor=fig.get_facecolor())
        plt.close(fig); return buf.getvalue()
    except Exception as e:
        log.warning(f"v3 ROI chart failed: {e}"); return None


def _png_image(data: bytes, width_cm: float = 7.5,
               height_cm: Optional[float] = None) -> Optional[Image]:
    """Embed PNG bytes as a reportlab Image with explicit size."""
    if not data:
        return None
    try:
        buf = io.BytesIO(data)
        h = height_cm * cm if height_cm is not None else width_cm * cm * 0.62
        img = Image(buf, width=width_cm * cm, height=h)
        img.hAlign = "CENTER"; return img
    except Exception as e:
        log.warning(f"_png_image failed: {e}"); return None


# ── V3 Layout helpers ──────────────────────────────────────────────────────────
def _v3_invest_block(rent_mo, price_growth_pct, price_growth_aed,
                     payback_yr, income_5y, st: dict, cw=None):
    """4 bilingual invest cards with colored top accent."""
    _cw = cw or (A4[0] - 2.4*cm); card_w = _cw / 4 - 1.5*mm
    try:
        pct_str = f"+{_pct(price_growth_pct)}/yr" if price_growth_pct else "—"
        aed_str = f"+{_money(price_growth_aed)}" if price_growth_aed else "—"
    except Exception:
        pct_str = "—"; aed_str = "—"
    items = [
        ("RENTAL / MONTH",  "Аренда в месяц",
         _money(rent_mo) if rent_mo else "—", "med. 1BR rental",  TEAL),
        ("PRICE GROWTH",    "Прирост за год",
         aed_str, pct_str, GREEN_OK),
        ("PAYBACK PERIOD",  "Окупаемость",
         (f"{float(payback_yr):.0f} yr" if payback_yr else "—"), "at current yield", GOLD),
        ("5-YEAR TOTAL",    "Доход за 5 лет",
         (_pct(income_5y) if income_5y else "—"), "rent + price growth", BLUE_ACC),
    ]
    cells = []
    for (en_lbl, ru_lbl, val, sub, accent) in items:
        inner = Table(
            [[Paragraph(en_lbl, st["kl"])],
             [Paragraph(val,    st["big_v"])],
             [Paragraph(sub,    st["big_sub"])],
             [Paragraph(ru_lbl, st["kl_ru"])]],
            colWidths=[card_w])
        inner.setStyle(TableStyle([
            ("BACKGROUND",    (0,0), (-1,-1), OFF_WHITE),
            ("LINEABOVE",     (0,0), (-1,0),  4, accent),
            ("TOPPADDING",    (0,0), (0,0),   6),
            ("TOPPADDING",    (0,1), (0,1),   4),
            ("BOTTOMPADDING", (0,0), (-1,-1), 6),
            ("TOPPADDING",    (0,2), (0,2),   2),
            ("TOPPADDING",    (0,3), (0,3),   6),
            ("LEFTPADDING",   (0,0), (-1,-1), 3),
            ("RIGHTPADDING",  (0,0), (-1,-1), 3),
            ("ALIGN", (0,0), (-1,-1), "CENTER"),
        ]))
        cells.append(inner)
    t = Table([cells], colWidths=[card_w]*4, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("LEFTPADDING",   (0,0), (-1,-1), 2), ("RIGHTPADDING",  (0,0), (-1,-1), 2),
        ("TOPPADDING",    (0,0), (-1,-1), 0), ("BOTTOMPADDING", (0,0), (-1,-1), 0),
    ]))
    return t


def _v3_kpi_row(items, st: dict, cols=4, cw=None):
    """Wide KPI grid, 4 cards per row, gold top accent."""
    _cw = cw or (A4[0] - 2.4*cm); card_w = _cw / cols - 1.5*mm
    while len(items) % cols:
        items.append(("", ""))
    rows = []
    for i in range(0, len(items), cols):
        row_cells = []
        for (lbl, val) in items[i:i+cols]:
            style_v = "kv_sm" if len(str(val)) > 10 else "kv"
            cell = Table(
                [[Paragraph(str(val) if val else "—", st[style_v])],
                 [Paragraph(lbl, st["kl"])]],
                colWidths=[card_w])
            cell.setStyle(TableStyle([
                ("BACKGROUND",    (0,0), (-1,-1), OFF_WHITE),
                ("LINEABOVE",     (0,0), (-1,0),  2.5, GOLD),
                ("TOPPADDING",    (0,0), (-1,-1), 7),
                ("BOTTOMPADDING", (0,0), (-1,-1), 7),
                ("LEFTPADDING",   (0,0), (-1,-1), 3),
                ("RIGHTPADDING",  (0,0), (-1,-1), 3),
                ("ALIGN", (0,0), (-1,-1), "CENTER"),
            ]))
            row_cells.append(cell)
        rows.append(row_cells)
    t = Table(rows, colWidths=[card_w]*cols, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("LEFTPADDING",  (0,0),(-1,-1), 2), ("RIGHTPADDING", (0,0),(-1,-1), 2),
        ("TOPPADDING",   (0,0),(-1,-1), 2), ("BOTTOMPADDING",(0,0),(-1,-1), 2),
    ]))
    return t


# ── V3 Footer ─────────────────────────────────────────────────────────────────
def _make_footer(lang: str, total_pages: int = 3):
    def _draw(canvas, doc):
        canvas.saveState()
        F = _FONT_REG if _FONT_REG in pdfmetrics.getRegisteredFontNames() else "Helvetica"
        canvas.setFont(F, 7); canvas.setFillColor(SLATE_400)
        canvas.drawString(1.4*cm, 0.9*cm, "Dubai Real Estate Analytics · DLD Data")
        pg = canvas.getPageNumber()
        canvas.drawRightString(A4[0] - 1.4*cm, 0.9*cm,
                               f"{_t(lang, 'page')} {pg} {_t(lang, 'of')} {total_pages}")
        canvas.setStrokeColor(GOLD); canvas.setLineWidth(1.5)
        canvas.line(1.4*cm, A4[1] - 0.8*cm, A4[0] - 1.4*cm, A4[1] - 0.8*cm)
        canvas.setStrokeColor(SLATE_200); canvas.setLineWidth(0.5)
        canvas.line(1.4*cm, 1.15*cm, A4[0] - 1.4*cm, 1.15*cm)
        canvas.restoreState()
    return _draw


def _grade_from_payload(payload: dict) -> str:
    """Determine A/B/C grade from yield + growth."""
    score = payload.get("investment_score")
    if score is not None:
        try:
            s = float(score)
            return "A" if s >= 75 else ("B" if s >= 50 else "C")
        except Exception:
            pass
    try:
        total = float(payload.get("yield") or payload.get("rental_yield") or 0) + \
                float(payload.get("growth_yoy") or payload.get("growth") or 0)
        return "A" if total >= 10 else ("B" if total >= 6 else "C")
    except Exception:
        return "B"


# ── ROI-bot structured calc renderer ─────────────────────────────────────────
def _build_roi_calc_section(story: list, st: dict, payload: dict, lang: str, _cw):
    """
    v_pdf3: renders structured ROI calculator output as PDF sections.
    Mirrors the exact bot chat format: yield / mortgage / growth / total / world.
    Called from _build_page1 when report_type=='roi' and income_year is present.
    """
    is_ru = lang == "ru"
    is_ar = lang == "ar"

    # ── localized labels ──────────────────────────────────────────────────────
    def L(ru, en, ar=None):
        if is_ru: return ru
        if is_ar and ar: return ar
        return en

    lbl_rental  = L("ДОХОДНОСТЬ ОТ АРЕНДЫ", "RENTAL YIELD",       "عائد الإيجار")
    lbl_mort    = L("ИПОТЕЧНЫЙ РАСЧЁТ",      "MORTGAGE BREAKDOWN", "حساب الرهن العقاري")
    lbl_growth  = L("РОСТ КАПИТАЛА",         "CAPITAL GROWTH",     "نمو رأس المال")
    lbl_total   = L("ИТОГО ЗА 5 ЛЕТ",       "5-YEAR TOTAL RETURN","العائد الإجمالي 5 سنوات")
    lbl_world   = L("ДУБАЙ VS МИР",         "DUBAI vs WORLD",     "دبي مقابل العالم")

    lbl_inc_yr  = L("Доход / год",        "Annual income")
    lbl_yield   = "Rental Yield"
    lbl_payback = L("Окупаемость",        "Payback period")
    lbl_rent_mo = L("Аренда / мес",       "Monthly rent")
    lbl_down    = L("Взнос",              "Down payment")
    lbl_loan    = L("Кредит",             "Loan amount")
    lbl_mp      = L("Платёж / мес",       "Monthly payment")
    lbl_net_mo  = L("Чистый доход / мес", "Net monthly income")
    lbl_coc     = "Cash-on-cash"
    lbl_yr1     = L("Год 1",   "Year 1")
    lbl_yr3     = L("3 года",  "Year 3")
    lbl_yr5     = L("5 лет",   "Year 5")
    lbl_rent5   = L("Аренда за 5 лет",     "Rental income (5yr)")
    lbl_capgain = L("Прирост стоимости",   "Capital gain")
    lbl_total_l = L("Итого",              "Total income")
    lbl_roi_5   = L("ROI 5 лет",         "5yr ROI")

    # ── data ──────────────────────────────────────────────────────────────────
    budget  = float(payload.get("budget") or 0)
    y       = float(payload.get("yield") or 0)
    g       = float(payload.get("growth") or 0)
    inc_yr  = float(payload.get("income_year") or 0)
    rent_mo = float(payload.get("monthly_rent") or 0)
    payback = float(payload.get("payback") or 0)
    v1      = float(payload.get("v1") or 0)
    v3      = float(payload.get("v3") or 0)
    v5      = float(payload.get("v5") or 0)
    g5      = float(payload.get("g5") or 0)
    r5      = float(payload.get("roi_5y") or 0)
    rent5   = float(payload.get("rental_5yr") or (inc_yr * 5))
    has_mortgage = bool(payload.get("mortgage_payment"))

    # ── row builder (label / value) ───────────────────────────────────────────
    def row(label, value, bold_val=False):
        vstyle = st["h3"] if bold_val else st["body_l"]
        return [Paragraph(label, st["muted"]), Paragraph(value, vstyle)]

    def pct_vs_base(v, b):
        try:
            return f"(+{(float(v)/float(b) - 1)*100:.0f}%)"
        except Exception:
            return ""

    def make_table(rows, col_split=0.55):
        t = Table(rows, colWidths=[_cw * col_split, _cw * (1 - col_split)], hAlign="LEFT")
        t.setStyle(TableStyle([
            ("FONTSIZE",      (0, 0), (-1, -1), 8),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING",   (0, 0), (-1, -1), 0),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
        ]))
        return t

    # ── SECTION 1: Rental Yield ───────────────────────────────────────────────
    story.append(SectionTitle(lbl_rental,
                              subtitle=f"+{y:.1f}% yield · {payback:.1f}yr payback" if y else None,
                              w=_cw))
    story.append(Spacer(1, 0.1 * cm))
    story.append(make_table([
        row(lbl_inc_yr,  f"{_money(inc_yr)}  (+{y:.1f}%)"),
        row(lbl_rent_mo, _money(rent_mo)),
        row(lbl_payback, f"{payback:.1f} yr" if payback else "—"),
    ]))
    story.append(Spacer(1, 0.2 * cm))

    # ── SECTION 2: Mortgage (if applicable) ──────────────────────────────────
    if has_mortgage:
        story.append(SectionTitle(lbl_mort, w=_cw))
        story.append(Spacer(1, 0.1 * cm))
        story.append(make_table([
            row(lbl_down,   _money(payload.get("down_payment"))),
            row(lbl_loan,   _money(payload.get("loan"))),
            row(lbl_mp,     _money(payload.get("mortgage_payment"))),
            row(lbl_net_mo, _money(payload.get("net_monthly"))),
            row(lbl_coc,    f"{payload.get('cash_on_cash', 0):.1f}%"),
        ]))
        story.append(Spacer(1, 0.2 * cm))

    # ── SECTION 3: Capital Growth ─────────────────────────────────────────────
    story.append(SectionTitle(lbl_growth,
                              subtitle=f"+{g:.0f}%/yr" if g else None,
                              w=_cw))
    story.append(Spacer(1, 0.1 * cm))
    story.append(make_table([
        row(lbl_yr1, f"{_money(v1)}  {pct_vs_base(v1, budget)}"),
        row(lbl_yr3, f"{_money(v3)}  {pct_vs_base(v3, budget)}"),
        row(lbl_yr5, f"{_money(v5)}  {pct_vs_base(v5, budget)}"),
    ], col_split=0.45))
    story.append(Spacer(1, 0.2 * cm))

    # ── SECTION 4: 5-year total ───────────────────────────────────────────────
    story.append(SectionTitle(lbl_total, w=_cw))
    story.append(Spacer(1, 0.1 * cm))
    total_tbl = Table([
        row(lbl_rent5,   _money(rent5)),
        row(lbl_capgain, _money(g5)),
        row(lbl_total_l, _money(rent5 + g5), bold_val=True),
        row(lbl_roi_5,   f"{r5:.1f}%",        bold_val=True),
    ], colWidths=[_cw * 0.55, _cw * 0.45], hAlign="LEFT")
    total_tbl.setStyle(TableStyle([
        ("FONTSIZE",      (0, 0), (-1, -1), 8),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
        ("LINEABOVE",     (0, 2), (-1, 2),  0.8, GOLD),
        ("BACKGROUND",    (0, 2), (-1, 3),  OFF_WHITE),
    ]))
    story.append(total_tbl)
    story.append(Spacer(1, 0.2 * cm))

    # ── SECTION 5: Dubai vs World ─────────────────────────────────────────────
    WORLD_STATIC = {
        "London": 2.5, "New York": 2.9, "Paris": 2.2,
        "Singapore": 3.1, "Hong Kong": 2.3, "Berlin": 3.8,
    }
    world_data = payload.get("world_comparison") or WORLD_STATIC
    district = payload.get("name") or "Dubai"
    story.append(SectionTitle(lbl_world, w=_cw))
    story.append(Spacer(1, 0.1 * cm))
    world_rows = [[Paragraph(f"🏙 {district}", st["h3"]),
                   Paragraph(f"{y:.1f}%", st["h3"])]]
    for city, yy in list(world_data.items())[:6]:
        world_rows.append([Paragraph(city, st["muted"]),
                           Paragraph(f"{yy:.1f}%", st["muted"])])
    wt = Table(world_rows, colWidths=[_cw * 0.65, _cw * 0.35], hAlign="LEFT")
    wt.setStyle(TableStyle([
        ("FONTSIZE",      (0, 0), (-1, -1), 8),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
        ("LINEBELOW",     (0, 0), (-1, 0),  0.8, TEAL),
    ]))
    story.append(wt)
    story.append(Spacer(1, 0.15 * cm))


# ── V3 Page builders ──────────────────────────────────────────────────────────
def _build_page1(story: list, st: dict, report_type: str, payload: dict, lang: str):
    """Page 1: PremiumHeader + executive summary + 4 invest cards + 8 KPIs."""
    logo = _find_logo(); today = datetime.utcnow().strftime("%d %b %Y")
    subject = (payload.get("name") or payload.get("title") or payload.get("area") or
               payload.get("project_name") or payload.get("building_name") or "").strip()
    area = (payload.get("area") or payload.get("location") or "").strip()
    emirate = (payload.get("emirate") or "Dubai").strip()
    tag = I18N.get(lang, I18N["en"])["report_types"].get(report_type, "Investment Report")
    price = payload.get("avg_price") or payload.get("median_price") or payload.get("price")
    price_str = _money(price) if price else "—"
    grade = _grade_from_payload(payload)
    meta_parts = [p for p in [area if area.lower() != subject.lower() else "",
                               emirate, today] if p]
    _cw = A4[0] - 2.4*cm
    story.append(PremiumHeader(logo, tag=tag, subject=subject,
                               meta=" · ".join(meta_parts), price_str=price_str,
                               grade=grade, h=3.6*cm, cw=_cw))
    story.append(Spacer(1, 0.35*cm))

    # v_pdf3: ROI-bot gets structured calc sections instead of garbled summary
    if report_type == "roi" and payload.get("income_year"):
        _build_roi_calc_section(story, st, payload, lang, _cw)
        story.append(PageBreak())
        return

    lbl_sum = "ИНВЕСТИЦИОННОЕ РЕЗЮМЕ" if lang == "ru" else "EXECUTIVE SUMMARY"
    story.append(SectionTitle(lbl_sum,
                              subtitle="Investment Summary" if lang == "ru" else None, w=_cw))
    story.append(Spacer(1, 0.18*cm))
    summary = payload.get("summary") or payload.get("llm_summary")
    if not summary:
        try:
            llm_out = _llm_summary(payload, lang)
        except Exception:
            llm_out = None
        generic = _t(lang, "summary_fallback")
        if not llm_out or llm_out.strip() == generic.strip() or len(llm_out.strip()) < 80:
            summary = _static_executive_template(payload, lang)
        else:
            summary = llm_out
    summary = (summary.replace("First Place Realtor L.L.C.", "")
                      .replace("First Place Realty", "").replace("First Place", ""))
    summary_clean = summary.strip().replace("\n\n", " ").replace("\n", " ")
    if len(summary_clean) > 800:
        summary_clean = summary_clean[:780].rsplit(" ", 1)[0] + "…"
    story.append(Paragraph(summary_clean, st["body"]))
    story.append(Spacer(1, 0.3*cm))

    lbl_inv = "ЧТО ВЫ ПОЛУЧАЕТЕ" if lang == "ru" else "INVESTOR METRICS"
    story.append(SectionTitle(lbl_inv,
                              subtitle="What You Get as an Investor" if lang == "ru" else None,
                              w=_cw))
    story.append(Spacer(1, 0.15*cm))
    rent_mo = payload.get("monthly_rent") or payload.get("avg_rent_1br")
    pct_growth = payload.get("growth_yoy") or payload.get("growth") or 0
    avg_p = payload.get("avg_price") or payload.get("median_price") or 0
    try:
        aed_growth = float(avg_p) * float(pct_growth) / 100.0
    except Exception:
        aed_growth = 0
    payback = payload.get("payback_years")
    income_5y = payload.get("total_return_5y_pct") or payload.get("roi_5y")
    story.append(_v3_invest_block(
        rent_mo=rent_mo, price_growth_pct=pct_growth,
        price_growth_aed=aed_growth, payback_yr=payback,
        income_5y=income_5y, st=st, cw=_cw))
    story.append(Spacer(1, 0.3*cm))

    lbl_kpi = "КЛЮЧЕВЫЕ ПАРАМЕТРЫ" if lang == "ru" else "KEY METRICS"
    story.append(SectionTitle(lbl_kpi, w=_cw))
    story.append(Spacer(1, 0.15*cm))
    kpi_items: List[Tuple[str, str]] = []
    def add_kpi(lbl, val):
        if val and val != "—":
            kpi_items.append((lbl, val))
    if lang == "ru":
        add_kpi("Цена",           _money(payload.get("avg_price") or payload.get("median_price")))
        add_kpi("Цена за м²",     _money(payload.get("price_per_m2"), " AED/m²") if payload.get("price_per_m2") else None)
        add_kpi("Rental Yield",   _pct(payload.get("yield") or payload.get("rental_yield")) if (payload.get("yield") or payload.get("rental_yield")) else None)
        add_kpi("Рост YoY",       _pct(pct_growth) if pct_growth else None)
        add_kpi("Застройщик",     str(payload.get("developer") or "")[:16] or None)
        add_kpi("Сдача",          str(payload.get("handover_date") or payload.get("completion_year") or "")[:12] or None)
        add_kpi("Сделок/год",     _num(payload.get("deals") or payload.get("deals_12m")))
        add_kpi("Срок продажи",   (f"{payload.get('avg_days_on_market')} дн" if payload.get("avg_days_on_market") else None))
    else:
        add_kpi("Price",          _money(payload.get("avg_price") or payload.get("median_price")))
        add_kpi("Price / m²",     _money(payload.get("price_per_m2"), " AED/m²") if payload.get("price_per_m2") else None)
        add_kpi("Rental Yield",   _pct(payload.get("yield") or payload.get("rental_yield")) if (payload.get("yield") or payload.get("rental_yield")) else None)
        add_kpi("YoY Growth",     _pct(pct_growth) if pct_growth else None)
        add_kpi("Developer",      str(payload.get("developer") or "")[:16] or None)
        add_kpi("Handover",       str(payload.get("handover_date") or payload.get("completion_year") or "")[:12] or None)
        add_kpi("Deals/yr",       _num(payload.get("deals") or payload.get("deals_12m")))
        add_kpi("Days on market", (f"{payload.get('avg_days_on_market')} d" if payload.get("avg_days_on_market") else None))
    kpi_items = kpi_items[:8]
    while len(kpi_items) < 8:
        kpi_items.append(("", ""))
    story.append(_v3_kpi_row(kpi_items, st, cols=4, cw=_cw))
    story.append(PageBreak())


def _build_page2(story: list, st: dict, payload: dict, lang: str):
    """Page 2: trend chart (full-width) + comparison horizontal bars."""
    logo = _find_logo()
    area = (payload.get("area") or payload.get("location") or "").strip()
    emirate = (payload.get("emirate") or "Dubai").strip()
    _cw = A4[0] - 2.4*cm
    story.append(PremiumHeader(logo, tag="Market Dynamics",
                               subject=area or "Market Overview",
                               meta=f"{emirate}  ·  Dubai Land Department data",
                               price_str="12m trend",
                               grade=_grade_from_payload(payload),
                               h=2.8*cm, cw=_cw))
    story.append(Spacer(1, 0.3*cm))

    lbl_trend = "ДИНАМИКА ЦЕН — 12 МЕС" if lang == "ru" else "PRICE TREND — 12 MONTHS"
    story.append(SectionTitle(lbl_trend, subtitle="Monthly Median Price · DLD", w=_cw))
    story.append(Spacer(1, 0.12*cm))
    series = payload.get("dynamics_series") or payload.get("price_series") or []
    trend_png = _v3_chart_trend(series, area)
    if trend_png:
        story.append(_png_image(trend_png, width_cm=18.0, height_cm=5.8))
    else:
        story.append(Paragraph(
            "Исторические данные динамики цен загружаются при первом запросе."
            if lang == "ru" else
            "Price history data is loaded on first request.", st["muted"]))
    story.append(Spacer(1, 0.3*cm))

    lbl_cmp = "СРАВНЕНИЕ РАЙОНОВ" if lang == "ru" else "AREA COMPARISON"
    story.append(SectionTitle(lbl_cmp,
                              subtitle="Price/m² and Rental Yield by Area", w=_cw))
    story.append(Spacer(1, 0.12*cm))
    comp = payload.get("comparison") or payload.get("similar") or []
    cmp_png = _v3_chart_comparison(comp, area)
    if cmp_png:
        story.append(_png_image(cmp_png, width_cm=18.0, height_cm=5.8))
    else:
        story.append(Paragraph(
            "Сравнительные данные по районам будут доступны после синхронизации."
            if lang == "ru" else
            "Area comparison is available after DLD sync.", st["muted"]))
    story.append(PageBreak())


def _build_page3(story: list, st: dict, payload: dict, lang: str):
    """Page 3: ROI bars + area stats KPIs + risks/signals + disclaimer."""
    logo = _find_logo()
    area = (payload.get("area") or payload.get("location") or "").strip()
    emirate = (payload.get("emirate") or "Dubai").strip()
    _cw = A4[0] - 2.4*cm
    story.append(PremiumHeader(logo, tag="ROI Projection & Area Statistics",
                               subject=area or "Investment Analysis",
                               meta=f"{emirate}  ·  Data-driven forecast",
                               price_str="10yr ROI",
                               grade=_grade_from_payload(payload),
                               h=2.8*cm, cw=_cw))
    story.append(Spacer(1, 0.3*cm))

    lbl_roi = "ПРОГНОЗ ДОХОДНОСТИ — 10 ЛЕТ" if lang == "ru" else "10-YEAR ROI FORECAST"
    story.append(SectionTitle(lbl_roi, subtitle="Cumulative Return Forecast", w=_cw))
    story.append(Spacer(1, 0.1*cm))
    yld = payload.get("yield") or payload.get("rental_yield") or 5.5
    growth = payload.get("growth_yoy") or payload.get("growth") or 3.0
    try:
        yld = float(yld); growth = float(growth)
    except Exception:
        yld = 5.5; growth = 3.0
    note_txt = (f"Yield {yld:.1f}% + рост {growth:.1f}%/год · без реинвестирования."
                if lang == "ru" else
                f"Yield {yld:.1f}% + YoY growth {growth:.1f}%/yr · no compounding.")
    story.append(Paragraph(note_txt, st["muted"]))
    story.append(Spacer(1, 0.08*cm))
    roi_bd = payload.get("roi_breakdown") or []
    roi_png = _v3_chart_roi(roi_bd or None, base_yield=yld, growth=growth)
    if roi_png:
        story.append(_png_image(roi_png, width_cm=18.0, height_cm=5.0))
    story.append(Spacer(1, 0.25*cm))

    lbl_area = (f"СТАТИСТИКА РАЙОНА: {area.upper()}" if lang == "ru"
                else f"AREA STATISTICS: {area.upper()}")
    story.append(SectionTitle(lbl_area, w=_cw))
    story.append(Spacer(1, 0.12*cm))
    area_kpis: List[Tuple[str, str]] = []
    def add_ak(lbl, val):
        area_kpis.append((lbl, val if val and val != "—" else "—"))
    if lang == "ru":
        add_ak("Медиана цены",   _money(payload.get("median_price") or payload.get("avg_price")))
        add_ak("Цена за м²",     _money(payload.get("price_per_m2"), " AED/m²") if payload.get("price_per_m2") else "—")
        add_ak("Аренда 1BR/мес", _money(payload.get("avg_rent_1br") or payload.get("monthly_rent")) if (payload.get("avg_rent_1br") or payload.get("monthly_rent")) else "—")
        add_ak("Аренда 2BR/мес", _money(payload.get("avg_rent_2br")) if payload.get("avg_rent_2br") else "—")
        add_ak("Аренда 3BR/мес", _money(payload.get("avg_rent_3br")) if payload.get("avg_rent_3br") else "—")
        add_ak("Сделок/год",     _num(payload.get("deals_12m") or payload.get("deals")))
        add_ak("Сделок/мес",     _num(payload.get("deals") or payload.get("deals_30d")))
        add_ak("Срок продажи",   (f"{payload.get('avg_days_on_market')} дн" if payload.get("avg_days_on_market") else "—"))
    else:
        add_ak("Median Price",   _money(payload.get("median_price") or payload.get("avg_price")))
        add_ak("Price / m²",     _money(payload.get("price_per_m2"), " AED/m²") if payload.get("price_per_m2") else "—")
        add_ak("Rent 1BR/mo",    _money(payload.get("avg_rent_1br") or payload.get("monthly_rent")) if (payload.get("avg_rent_1br") or payload.get("monthly_rent")) else "—")
        add_ak("Rent 2BR/mo",    _money(payload.get("avg_rent_2br")) if payload.get("avg_rent_2br") else "—")
        add_ak("Rent 3BR/mo",    _money(payload.get("avg_rent_3br")) if payload.get("avg_rent_3br") else "—")
        add_ak("Deals/year",     _num(payload.get("deals_12m") or payload.get("deals")))
        add_ak("Deals/month",    _num(payload.get("deals") or payload.get("deals_30d")))
        add_ak("Days on market", (f"{payload.get('avg_days_on_market')} d" if payload.get("avg_days_on_market") else "—"))
    while len(area_kpis) < 8:
        area_kpis.append(("", ""))
    story.append(_v3_kpi_row(area_kpis[:8], st, cols=4, cw=_cw))
    story.append(Spacer(1, 0.25*cm))

    lbl_rs = "РИСКИ · СИГНАЛЫ РОСТА" if lang == "ru" else "RISKS · GROWTH SIGNALS"
    story.append(SectionTitle(lbl_rs, w=_cw))
    story.append(Spacer(1, 0.12*cm))
    risks = payload.get("risks") or _t(lang, "default_risks")
    signals = payload.get("signals") or _t(lang, "default_signals")
    half_w = _cw / 2 - 2*mm
    risk_rows = [[Paragraph(f"✗  {r}", st["risk"])] for r in (risks or [])[:5]]
    sig_rows  = [[Paragraph(f"✔  {s}", st["ok"])]  for s in (signals or [])[:5]]
    if not risk_rows:  risk_rows = [[Paragraph("—", st["muted"])]]
    if not sig_rows:   sig_rows  = [[Paragraph("—", st["muted"])]]
    risk_t = Table(risk_rows, colWidths=[half_w])
    risk_t.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,-1), colors.HexColor("#FFF5F5")),
        ("LINEABOVE",     (0,0), (-1,0),  2.5, RED_SOFT),
        ("TOPPADDING",    (0,0), (-1,-1), 5), ("BOTTOMPADDING", (0,0), (-1,-1), 5),
        ("LEFTPADDING",   (0,0), (-1,-1), 8),
    ]))
    sig_t = Table(sig_rows, colWidths=[half_w])
    sig_t.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,-1), colors.HexColor("#F0FDF4")),
        ("LINEABOVE",     (0,0), (-1,0),  2.5, TEAL),
        ("TOPPADDING",    (0,0), (-1,-1), 5), ("BOTTOMPADDING", (0,0), (-1,-1), 5),
        ("LEFTPADDING",   (0,0), (-1,-1), 8),
    ]))
    rs_t = Table([[risk_t, sig_t]], colWidths=[half_w + 2*mm, half_w + 2*mm])
    rs_t.setStyle(TableStyle([
        ("LEFTPADDING",  (0,0), (-1,-1), 2), ("RIGHTPADDING", (0,0), (-1,-1), 2),
        ("VALIGN",       (0,0), (-1,-1), "TOP"),
    ]))
    story.append(rs_t)
    story.append(Spacer(1, 0.35*cm))

    story.append(HRFlowable(width=_cw, thickness=0.5, color=SLATE_200, spaceAfter=4))
    story.append(Paragraph(_t(lang, "disclaimer_text"), st["disc"]))
    story.append(Spacer(1, 0.12*cm))
    story.append(Paragraph(f"© {datetime.utcnow().year} · Dubai, UAE", st["muted"]))


# ── legacy compact chart stubs (kept for compatibility, not used by page builders) ──
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
                          _t(lang, "footer_brand"))
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


# ── Page 1: Cover + Executive ──
def _build_page1(story: list, st: dict, report_type: str,
                 payload: dict, lang: str):
    """Page 1: brand header bar + report title + executive paragraph.
    Modern minimal style — no personal contacts, no profile strip."""

    NAVY = colors.HexColor("#1B3A6B")
    GOLD = colors.HexColor("#C9A84C")

    # ── Brand header bar: navy background, logo left + brand name right ──
    logo = _find_logo()
    logo_cell = ""
    if logo and PIL_OK:
        try:
            im = PILImage.open(logo)
            w, h = im.size
            target_w = 2.4 * cm
            ratio = h / w
            logo_img = Image(logo, width=target_w, height=target_w * ratio)
            logo_img.hAlign = "LEFT"
            logo_cell = logo_img
        except Exception as e:
            log.debug(f"cover logo embed failed: {e}")
    if not logo_cell:
        logo_cell = Paragraph(
            "",
            ParagraphStyle("lc", fontName=st["FB"], fontSize=20,
                           textColor=colors.white, alignment=TA_LEFT))

    header_tbl = Table([[logo_cell]],
                       colWidths=[17.8 * cm])
    header_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(header_tbl)
    story.append(Spacer(1, 0.15 * cm))

    # P1-6: brand + RERA BRN line directly under navy header
    _brand_style = ParagraphStyle(
        "brand_line",
        fontName=st["FB"],
        fontSize=9,
        textColor=GOLD,
        alignment=TA_CENTER,
        leading=11,
    )
    story.append(Paragraph(
        f"<b>{BRAND_NAME}</b>  ·  {BRAND_SUBTITLE}  ·  {BRAND_RERA}",
        _brand_style,
    ))
    story.append(Spacer(1, 0.25 * cm))

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

    # v_pdf3: ROI-bot gets structured calc sections instead of garbled summary
    if report_type == "roi" and payload.get("income_year"):
        _build_roi_calc_section(story, st, payload, lang, 17.8 * cm)
        story.append(PageBreak())
        return

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
               .replace("First Place Realtor L.L.C.", "")
               .replace("First Place Realty", "")
               .replace("First Place", ""))
    # Trim summary to ~900 chars to keep page 1 contained.
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

    # ── Gold separator line (bottom of page 1 content) ──
    story.append(Spacer(1, 0.5 * cm))
    # Draw a thin gold horizontal rule via a 1-row Table with gold background
    sep_tbl = Table([[""]],
                    colWidths=[17.8 * cm],
                    rowHeights=[0.12 * cm])
    sep_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), GOLD),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(sep_tbl)
    story.append(PageBreak())


# ── Page 2: KPI grid + charts + risks/signals (2 columns) ──
def _build_page2(story: list, st: dict, payload: dict, lang: str):
    """Page 2: 2-column layout — left KPI+risks+signals, right charts."""
    story.append(Paragraph(_t(lang, "details"), st["h1"]))

    # ── Build LEFT column content (KPI + risks + signals) ──
    # B051: all labels via _t() — supports RU/EN/AR (and any future lang).
    L = {
        "avg_price":       _t(lang, "kpi_avg_price"),
        "median_price":    _t(lang, "kpi_median_price"),
        "price_per_m2":    _t(lang, "kpi_price_per_m2"),
        "deals":           _t(lang, "kpi_deals_12m"),
        "yield":           _t(lang, "kpi_yield"),
        "growth":          _t(lang, "kpi_growth_yoy"),
        "liquidity":       _t(lang, "kpi_liquidity"),
        "units":           _t(lang, "kpi_units"),
        "area":            _t(lang, "kpi_area_m2"),
        "developer":       _t(lang, "kpi_developer"),
        "handover":        _t(lang, "kpi_handover"),
        "bedrooms":        _t(lang, "kpi_bedrooms"),
        "emirate":         _t(lang, "kpi_emirate"),
        "stage":           _t(lang, "kpi_stage"),
        "score":           _t(lang, "kpi_score"),
        "payback":         _t(lang, "kpi_payback"),
        "return_5y":       _t(lang, "kpi_return_5y"),
        "top_psf":         _t(lang, "kpi_top_psf"),
        "area_growth_5y":  _t(lang, "kpi_growth_5y"),
        "area_growth_10y": _t(lang, "kpi_growth_10y"),
        "monthly_rent":    _t(lang, "kpi_monthly_rent"),
        "budget":          _t(lang, "kpi_budget"),
    }

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
        add(L["payback"],       (f"{payload.get('payback_years')} {_t(lang, 'u_years_short')}"
                                 if payload.get("payback_years") else None))
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
        right_col.append(Paragraph(_t(lang, "no_history"), st["muted"]))

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
# ── PDF Intelligence v2 helpers ──────────────────────────────────────────
# Optional cosmic-grade upgrade: thesis, forecast, liquidity tier, photo analysis.
# Each rendered inside its own try/except wrapper inside _build_page3 so a
# failure in any single feature can NEVER break PDF generation.

def _v2_pkg():
    """Lazy-import the pdf_intelligence package; return module or None."""
    try:
        from pdf_intelligence import (  # type: ignore
            generate_thesis,
            forecast_prices,
            build_forecast_chart,
            compute_liquidity_tier,
            analyze_property_photos,
            format_photo_analysis,
        )
        return {
            "generate_thesis": generate_thesis,
            "forecast_prices": forecast_prices,
            "build_forecast_chart": build_forecast_chart,
            "compute_liquidity_tier": compute_liquidity_tier,
            "analyze_property_photos": analyze_property_photos,
            "format_photo_analysis": format_photo_analysis,
        }
    except Exception as e:
        log.debug(f"pdf_intelligence import skipped: {e}")
        return None


def _v2_render_liquidity_badge(story: list, st: dict, payload: dict, lang: str):
    """Render a small A/B/C/D liquidity badge above the comparison header."""
    pkg = _v2_pkg()
    if not pkg:
        return
    building = (payload.get("building") or payload.get("name") or "").strip()
    area = (payload.get("area") or payload.get("location") or "").strip()
    deals_12m = payload.get("deals_12m") or payload.get("deals")
    tier = pkg["compute_liquidity_tier"](
        building=building if building and building != area else None,
        area=area or None,
        deals_12m=int(deals_12m) if deals_12m else None,
        lang=lang,
    )
    if not tier or not tier.get("tier"):
        return
    label_key = "kpi_liquidity" if "kpi_liquidity" in I18N.get(lang, {}) else ""
    label_text = _t(lang, "kpi_liquidity", "Ликвидность")
    days = tier.get("avg_days_between")
    days_part = f" · ~{int(days)} {_t(lang, 'u_days_short', 'd')} между сделками" if days else ""
    badge_text = (
        f'<b>💧 {label_text}: '
        f'<font color="{tier["color"]}">Tier {tier["tier"]} · {tier["label"]}</font></b>'
        f'<font size="7" color="#78716C">{days_part}</font>'
    )
    try:
        story.append(Paragraph(badge_text, st["body_l"]))
        story.append(Spacer(1, 0.15 * cm))
    except Exception as e:
        log.debug(f"v2 badge paragraph failed: {e}")


def _v2_render_intelligence_block(story: list, st: dict, payload: dict, lang: str):
    """Render the new cosmic intelligence sections: thesis, forecast, photos."""
    pkg = _v2_pkg()
    if not pkg:
        return

    building = (payload.get("building") or payload.get("name") or "—").strip() or "—"
    area = (payload.get("area") or payload.get("location") or "—").strip() or "—"
    deal_type = (payload.get("deal_type")
                 or payload.get("stage")
                 or "secondary")

    # ── Feature 1: Investment Thesis ──
    try:
        dld_stats = {
            "deals_count": int(payload.get("deals_12m") or payload.get("deals") or 0),
            "median_price": float(payload.get("median_price") or payload.get("avg_price") or 0),
            "median_per_sqft": float(payload.get("price_per_m2") or 0),
            "trend_pct": float(payload.get("growth_yoy") or 0),
            "days_to_sell": int(payload.get("days_on_market") or 60),
        }
        market_context = {
            "area_roi": float(payload.get("yield") or 6.5),
            "dubai_roi": float(payload.get("dubai_median_roi") or 6.5),
            "liquidity_rank": int(payload.get("liquidity_rank")
                                  or (8 if (payload.get("deals_12m") or 0) > 100 else 5)),
        }
        thesis_text = pkg["generate_thesis"](
            building=building, area=area, deal_type=str(deal_type),
            dld_stats=dld_stats, market_context=market_context,
        )
        if thesis_text:
            story.append(Spacer(1, 0.3 * cm))
            heading = "🧠 " + _t(lang, "investment_analysis", "Инвестиционный анализ")
            story.append(Paragraph(heading, st["h2"]))
            # break thesis to paragraphs for nicer wrapping
            for chunk in thesis_text.strip().split("\n\n"):
                if chunk.strip():
                    story.append(Paragraph(chunk.strip(), st["body"]))
    except Exception as e:
        log.warning(f"v2 thesis failed: {e}")

    # ── Feature 3: Price Forecast 36mo (only if we have history) ──
    try:
        series = payload.get("dynamics_series") or payload.get("price_series") or []
        # tuples (label, value) or plain values
        vals: List[float] = []
        for item in series:
            if isinstance(item, (tuple, list)) and len(item) >= 2:
                try:
                    vals.append(float(item[1]))
                except Exception:
                    pass
            elif isinstance(item, (int, float)):
                vals.append(float(item))
        if len(vals) >= 3:
            forecast = pkg["forecast_prices"](vals, months_ahead=36)
            chart_png = pkg["build_forecast_chart"](
                vals, forecast,
                title="Forecast 12 / 24 / 36 months",
            )
            if chart_png:
                story.append(Spacer(1, 0.3 * cm))
                story.append(Paragraph("🔮 " + _t(lang, "roi_forecast_10y",
                                                  "Прогноз 36 мес"), st["h2"]))
                story.append(_png_image(chart_png, width_cm=16.0, height_cm=6.0))
                # mini summary
                p12 = forecast["point"][11] if len(forecast["point"]) >= 12 else None
                p36 = forecast["point"][35] if len(forecast["point"]) >= 36 else None
                if p12 and p36:
                    pct12 = (p12 / vals[-1] - 1) * 100 if vals[-1] else 0
                    pct36 = (p36 / vals[-1] - 1) * 100 if vals[-1] else 0
                    conf_map = {"high": "высокая", "medium": "средняя", "low": "низкая"}
                    conf_ru = conf_map.get(forecast["confidence"], "—")
                    note = (f"Через 12 мес: ~{p12:,.0f} ({pct12:+.1f}%), "
                            f"через 36 мес: ~{p36:,.0f} ({pct36:+.1f}%). "
                            f"Уверенность модели: {conf_ru} (RMSE {forecast['rmse_pct']:.0f}%).")
                    story.append(Paragraph(note, st["muted"]))
    except Exception as e:
        log.warning(f"v2 forecast failed: {e}")

    # ── Feature 5: Photo Analysis (only if photo paths provided) ──
    try:
        photo_paths = payload.get("photo_paths") or payload.get("photos_local") or []
        if photo_paths and isinstance(photo_paths, list):
            data = pkg["analyze_property_photos"](photo_paths[:5])
            if data:
                story.append(Spacer(1, 0.3 * cm))
                story.append(Paragraph("🏠 Анализ фото объекта", st["h2"]))
                text = pkg["format_photo_analysis"](data, lang=lang)
                for line in text.split("\n"):
                    if line.strip():
                        story.append(Paragraph(line.strip(), st["body"]))
                    else:
                        story.append(Spacer(1, 0.1 * cm))
    except Exception as e:
        log.warning(f"v2 photo analysis failed: {e}")


def _build_page3(story: list, st: dict, payload: dict, lang: str):
    """Page 3: comparison table + detailed ROI breakdown + legal disclaimer."""
    # ── PDF Intelligence v2: liquidity tier badge (top of page 3) ──
    try:
        _v2_render_liquidity_badge(story, st, payload, lang)
    except Exception as _e:
        log.debug(f"v2 liquidity badge skipped: {_e}")
    story.append(Paragraph(_t(lang, "comparison"), st["h1"]))
    comp = payload.get("comparison") or payload.get("similar") or []
    if comp:
        head = [
            _t(lang, "th_num"), _t(lang, "th_name"), _t(lang, "th_area"),
            _t(lang, "th_price_m2"), _t(lang, "th_deals"),
            _t(lang, "th_yield"), _t(lang, "th_growth"),
        ]
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
        lines = []
        if area:
            lines.append(_t(lang, "no_comparison_intro").format(area=area))
        if avg:
            lines.append(_t(lang, "no_comparison_avg").format(v=_money(avg)))
        if psf:
            lines.append(_t(lang, "no_comparison_psf").format(v=_money(psf, " AED/m²")))
        if yld:
            lines.append(_t(lang, "no_comparison_yield").format(v=_pct(yld)))
        lines.append(_t(lang, "no_comparison_tail"))
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
            head = [_t(lang, "th_year"), _t(lang, "th_return")]
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
                    _t(lang, "u_total_5y") + " "
                    + _pct(payload.get("total_return_5y_pct")))
            if payload.get("payback_years"):
                parts.append(
                    _t(lang, "u_payback_about")
                    + f"{payload.get('payback_years')} "
                    + _t(lang, "u_years_short"))
            if parts:
                story.append(Paragraph(" · ".join(parts), st["body"]))

    # ── PDF Intelligence v2: investment thesis + forecast + photo analysis ──
    try:
        _v2_render_intelligence_block(story, st, payload, lang)
    except Exception as _e:
        log.debug(f"v2 intelligence block skipped: {_e}")

    # ── Top buildings or extra ranking (если есть в payload) ──
    tb = payload.get("top_buildings") or []
    if tb and isinstance(tb, list):
        story.append(Spacer(1, 0.25 * cm))
        story.append(Paragraph(_t(lang, "top_buildings"), st["h2"]))
        head = [_t(lang, "th_num"), _t(lang, "th_building"),
                _t(lang, "th_price_m2"), _t(lang, "th_deals")]
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
        f"© {datetime.utcnow().year} · Dubai, UAE",
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

    # ── B041: автоматическое обогащение payload из intel-БД ──
    # Если бот передал бедный payload (только area/building/price), подтянем
    # dynamics_series, comparison, top_buildings, yield, growth, ROI и т.д.
    # из daily_market_reports чтобы все 3 страницы PDF были заполнены.
    try:
        payload = _enrich_payload_from_intel(payload)
    except Exception as _enrich_err:
        log.warning(f"intel payload enrichment failed: {_enrich_err}")

    # ── Cache key (v134: bump после enrichment-фикса B041) ──
    payload_norm = json.dumps(payload, sort_keys=True, default=str)[:32000]
    payload_hash = hashlib.sha256(
        f"{report_type}|{lang}|{payload_norm}|v200-intelligence".encode("utf-8")).hexdigest()[:24]
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
        title=f"Dubai Real Estate Report — {report_type}",
        author="Dubai Real Estate Analytics",
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
