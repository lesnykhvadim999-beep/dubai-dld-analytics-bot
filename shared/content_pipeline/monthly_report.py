"""
Layer 19 — Monthly PDF report + summary post.

Использует существующий shared/vadim_pdf.py если он есть; иначе fallback на ReportLab.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from ._llm import chat
from ._queue import enqueue
from ._assets import pollinations_image

log = logging.getLogger("content_pipeline.monthly")

REPORT_DIR = Path(os.getenv("MONTHLY_REPORT_DIR", "C:/Projects/shared/content_pipeline/_reports"))
REPORT_DIR.mkdir(parents=True, exist_ok=True)
TARGET_CHAT = os.getenv("CHANNEL_TARGET", "@vadim_realty_channel")


def _build_pdf(month_label: str, summary_md: str) -> Optional[str]:
    """Сначала пробуем общий vadim_pdf, иначе ReportLab."""
    out = REPORT_DIR / f"monthly_{month_label}.pdf"
    try:
        from shared.vadim_pdf import build_monthly_report  # type: ignore
        path = build_monthly_report(out_path=str(out), summary_md=summary_md,
                                    month_label=month_label)
        return path if path and Path(path).exists() else None
    except Exception as e:
        log.info("vadim_pdf not used (%s), falling back to ReportLab", e)
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
        doc = SimpleDocTemplate(str(out), pagesize=A4)
        styles = getSampleStyleSheet()
        story = [
            Paragraph(f"<b>Vadim Realty — Monthly Report {month_label}</b>", styles["Title"]),
            Spacer(1, 12),
        ]
        for para in summary_md.split("\n\n"):
            story.append(Paragraph(para.replace("\n", "<br/>"), styles["Normal"]))
            story.append(Spacer(1, 8))
        story.append(Paragraph("Vadim Realty · RERA BRN 65011", styles["Italic"]))
        doc.build(story)
        return str(out)
    except Exception as e:  # noqa: BLE001
        log.exception("ReportLab fallback failed: %s", e)
        return None


def generate_monthly_report(target_chat: Optional[str] = None) -> Optional[int]:
    month_label = datetime.now(timezone.utc).strftime("%Y-%m")
    summary = chat(
        f"Составь итог месяца {month_label} по рынку недвижимости Дубая: "
        "5 разделов (Спрос, Предложение, Цены, Топ-районы, Прогноз). "
        "Каждый раздел 2-3 предложения. Подпись 'Vadim Realty · RERA BRN 65011'.",
        max_tokens=900, temperature=0.4,
    ) or "Отчёт временно недоступен."
    pdf_path = _build_pdf(month_label, summary)
    cover = pollinations_image(
        f"Dubai monthly market report cover, {month_label}, real-estate analytics, blue palette",
        width=1280, height=720, seed=int(month_label.replace("-", "")),
    )

    media: list[str] = []
    if cover:
        media.append(cover)
    if pdf_path:
        media.append(pdf_path)

    qid = enqueue(
        kind="monthly_report",
        target_chat=target_chat or TARGET_CHAT,
        body_md=f"📊 *Отчёт за {month_label}*\n\n{summary}",
        title=f"Monthly report {month_label}",
        media_paths=media,
        payload={"month": month_label, "pdf": pdf_path},
    )
    log.info("monthly_report enqueued id=%s", qid)
    return qid


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("queue_id =", generate_monthly_report())
