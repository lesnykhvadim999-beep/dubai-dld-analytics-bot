# -*- coding: utf-8 -*-
"""
subscription.py — shared monetisation layer for all Vadim Realty bots.

Features gated (10 free uses each, then paid):
  smart_pick   — AI Smart Pick wizard
  pdf          — Investment PDF report
  deals        — DLD deal history / specific transactions
  seller_data  — Seller contact details on resale listings

Plans:
  monthly  — $5/month  (30 days)   | 250 Stars | 2 TON
  yearly   — $50/year  (365 days)  | 2500 Stars | 20 TON  (saves $10 vs 12×$5)

Payment methods:
  stars  — Telegram Stars (XTR, no bank needed)
  ton    — TON via @CryptoBot API
"""

import os
from datetime import datetime, timedelta
from typing import Optional

FREE_LIMIT        = 10

# Monthly plan
SUB_DAYS_MONTH    = 30
STARS_PRICE_MONTH = 250        # ~$5
TON_AMOUNT_MONTH  = "2"        # ~$5
USD_PRICE_MONTH   = 5.0

# Yearly plan
SUB_DAYS_YEAR     = 365
STARS_PRICE_YEAR  = 2500       # ~$50  (saves $10 vs 12×250)
TON_AMOUNT_YEAR   = "20"       # ~$50
USD_PRICE_YEAR    = 50.0

# Legacy aliases (monthly defaults)
SUB_DAYS     = SUB_DAYS_MONTH
STARS_PRICE  = STARS_PRICE_MONTH
TON_AMOUNT   = TON_AMOUNT_MONTH
USD_PRICE    = USD_PRICE_MONTH

CRYPTOBOT_TOKEN = os.getenv("CRYPTOBOT_TOKEN", "")
CRYPTOBOT_API   = "https://pay.crypt.bot/api"

# --------------------------------------------------------------------------- #
# Feature display names (EN / RU)
# --------------------------------------------------------------------------- #
FEATURE_LABELS = {
    "smart_pick":  {"en": "AI Smart Pick",       "ru": "AI Подбор"},
    "pdf":         {"en": "Investment PDF",       "ru": "Инвест-PDF"},
    "deals":       {"en": "Deal History",         "ru": "История сделок DLD"},
    "seller_data": {"en": "Seller Contacts",      "ru": "Контакты продавца"},
}


# --------------------------------------------------------------------------- #
# DB helpers (raw psycopg2 connection — works from any bot)
# --------------------------------------------------------------------------- #

def is_premium(user_id: int, conn) -> bool:
    """Return True if user has an active subscription."""
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM user_subscriptions "
        "WHERE user_id = %s AND expires_at > NOW() LIMIT 1",
        (user_id,),
    )
    return cur.fetchone() is not None


def get_feature_count(user_id: int, feature: str, conn) -> int:
    """How many times has this user used this feature (free quota)."""
    cur = conn.cursor()
    cur.execute(
        "SELECT count FROM free_usage WHERE user_id = %s AND feature = %s",
        (user_id, feature),
    )
    row = cur.fetchone()
    return row[0] if row else 0


def consume_free(user_id: int, feature: str, conn) -> bool:
    """
    Try to consume one free use.
    Returns True  → allowed (was under FREE_LIMIT, counter incremented).
    Returns False → limit reached, user must subscribe.
    """
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO free_usage (user_id, feature, count)
        VALUES (%s, %s, 1)
        ON CONFLICT (user_id, feature)
        DO UPDATE SET count = free_usage.count + 1
        RETURNING count
        """,
        (user_id, feature),
    )
    new_count = cur.fetchone()[0]
    conn.commit()
    if new_count > FREE_LIMIT:
        cur.execute(
            "UPDATE free_usage SET count = %s WHERE user_id = %s AND feature = %s",
            (FREE_LIMIT, user_id, feature),
        )
        conn.commit()
        return False
    return True


def check_gate(user_id: int, feature: str, conn) -> bool:
    """
    Master gate check.
    Returns True  → proceed (premium OR free quota available).
    Returns False → blocked, show paywall.
    """
    if is_premium(user_id, conn):
        return True
    return consume_free(user_id, feature, conn)


def activate_subscription(
    user_id: int,
    method: str,
    tx_id: str,
    conn,
    days: int = SUB_DAYS_MONTH,
    amount_stars: Optional[int] = None,
    amount_usd: Optional[float] = None,
):
    """Record a successful payment and activate/extend subscription."""
    cur = conn.cursor()
    cur.execute(
        "SELECT expires_at FROM user_subscriptions "
        "WHERE user_id = %s AND expires_at > NOW() "
        "ORDER BY expires_at DESC LIMIT 1",
        (user_id,),
    )
    row = cur.fetchone()
    base = row[0] if row else datetime.utcnow()
    new_expiry = base + timedelta(days=days)

    cur.execute(
        """
        INSERT INTO user_subscriptions
            (user_id, plan, payment_method, amount_stars, amount_usd, tx_id, expires_at)
        VALUES (%s, 'premium', %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (user_id, method, amount_stars, amount_usd, tx_id, new_expiry),
    )
    conn.commit()
    return new_expiry


def get_subscription_info(user_id: int, conn) -> dict:
    """Return subscription status dict for /status command."""
    cur = conn.cursor()
    cur.execute(
        "SELECT expires_at, payment_method FROM user_subscriptions "
        "WHERE user_id = %s AND expires_at > NOW() "
        "ORDER BY expires_at DESC LIMIT 1",
        (user_id,),
    )
    sub = cur.fetchone()

    cur.execute(
        "SELECT feature, count FROM free_usage WHERE user_id = %s",
        (user_id,),
    )
    usage = {r[0]: r[1] for r in cur.fetchall()}

    return {
        "premium":    sub is not None,
        "expires_at": sub[0] if sub else None,
        "method":     sub[1] if sub else None,
        "usage":      usage,
    }


# --------------------------------------------------------------------------- #
# Paywall UI helpers
# --------------------------------------------------------------------------- #

def paywall_message(user_id: int, feature: str, lang: str, conn) -> str:
    """Full paywall text to send when gate blocks."""
    label = FEATURE_LABELS.get(feature, {}).get(lang, feature)

    if lang == "ru":
        return (
            f"🔒 *{label}* — лимит исчерпан\n\n"
            f"Вы использовали все {FREE_LIMIT} бесплатных запросов.\n\n"
            f"Подключите *Vadim Realty Pro* и получите:\n"
            f"• Неограниченный AI Подбор\n"
            f"• Все инвест-PDF отчёты\n"
            f"• Полная история сделок DLD\n"
            f"• Контакты продавцов\n\n"
            f"💳 *Тарифы:*\n"
            f"• Месяц — *$5* (250 Stars или 2 TON)\n"
            f"• Год — *$50* (2500 Stars или 20 TON) ✨ _Экономия $10_\n\n"
            f"Выберите план:"
        )
    return (
        f"🔒 *{label}* — free limit reached\n\n"
        f"You've used all {FREE_LIMIT} free uses for this feature.\n\n"
        f"Subscribe to *Vadim Realty Pro* and get:\n"
        f"• Unlimited AI Smart Pick\n"
        f"• All investment PDF reports\n"
        f"• Full DLD deal history\n"
        f"• Seller contact details\n\n"
        f"💳 *Plans:*\n"
        f"• Monthly — *$5/mo* (250 Stars or 2 TON)\n"
        f"• Yearly — *$50/yr* (2500 Stars or 20 TON) ✨ _Save $10_\n\n"
        f"Choose your plan:"
    )


def paywall_keyboard_inline():
    """
    Returns list-of-lists for an inline keyboard with payment buttons.
    Bot-specific code wraps these into InlineKeyboardButton objects.
    """
    return [
        [
            ("⭐ Monthly · 250 Stars", "sub|stars_month"),
            ("⭐ Yearly · 2500 Stars", "sub|stars_year"),
        ],
        [
            ("💎 Monthly · 2 TON",  "sub|ton_month"),
            ("💎 Yearly · 20 TON",  "sub|ton_year"),
        ],
        [
            ("ℹ️ What's included?", "sub|info"),
        ],
    ]


# --------------------------------------------------------------------------- #
# CryptoBot invoice creation
# --------------------------------------------------------------------------- #

def _cryptobot_invoice(user_id: int, asset: str, amount: str,
                        description: str) -> Optional[str]:
    """Generic CryptoBot invoice helper. Returns pay_url or None."""
    if not CRYPTOBOT_TOKEN:
        return None
    try:
        import urllib.request, json
        payload = json.dumps({
            "asset":       asset,
            "amount":      amount,
            "description": description,
            "payload":     str(user_id),
            "paid_btn_name": "callback",
            "paid_btn_url":  "https://t.me/VadimDubaiBot",
            "allow_comments":  False,
            "allow_anonymous": False,
        }).encode()
        req = urllib.request.Request(
            f"{CRYPTOBOT_API}/createInvoice",
            data=payload,
            headers={
                "Crypto-Pay-API-Token": CRYPTOBOT_TOKEN,
                "Content-Type": "application/json",
            },
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        if resp.get("ok"):
            return resp["result"]["pay_url"]
    except Exception as e:
        print(f"[subscription] CryptoBot invoice error: {e}")
    return None


def create_cryptobot_invoice(user_id: int) -> Optional[str]:
    """Monthly TON invoice (~2 TON ≈ $5)."""
    return _cryptobot_invoice(
        user_id, "TON", TON_AMOUNT_MONTH,
        "Vadim Realty Pro — 1 month",
    )


def create_cryptobot_invoice_year(user_id: int) -> Optional[str]:
    """Yearly TON invoice (~20 TON ≈ $50)."""
    return _cryptobot_invoice(
        user_id, "TON", TON_AMOUNT_YEAR,
        "Vadim Realty Pro — 1 year (save $10)",
    )


# --------------------------------------------------------------------------- #
# Stars invoice payload builders
# --------------------------------------------------------------------------- #

def stars_invoice_payload(user_id: int) -> dict:
    """Monthly Stars invoice (250 XTR ≈ $5)."""
    return {
        "title":          "Vadim Realty Pro — 1 Month",
        "description":    "30-day subscription: AI Pick · PDF · Deals · Seller data",
        "payload":        f"sub_stars_month_{user_id}",
        "provider_token": "",
        "currency":       "XTR",
        "prices":         [{"label": "Vadim Realty Pro 30d", "amount": STARS_PRICE_MONTH}],
    }


def stars_invoice_payload_year(user_id: int) -> dict:
    """Yearly Stars invoice (2500 XTR ≈ $50, saves $10)."""
    return {
        "title":          "Vadim Realty Pro — 1 Year",
        "description":    "365-day subscription: AI Pick · PDF · Deals · Seller data · Save $10",
        "payload":        f"sub_stars_year_{user_id}",
        "provider_token": "",
        "currency":       "XTR",
        "prices":         [{"label": "Vadim Realty Pro 365d", "amount": STARS_PRICE_YEAR}],
    }
