"""
safe_coerce.py — единые SQL-хелперы для безопасного приведения типов
из шумных DLD/listings источников.

Корневая причина регрессии B055 (Grande / Burj Khalifa "нет данных"):
    SQL-фрагмент   col::text ~ '^\\d{4}-\\d{2}-\\d{2}'   THEN col::date
    не матчит формат  DD-MM-YYYY  →  safe_date = NULL  →  фильтр периода
    всё отрезает → юзер видит "нет данных".

Решение: использовать ОДИН helper во всех ботах. Если когда-то DLD
добавит новый формат — правим safe_date_sql() здесь, и все боты
получают фикс одновременно.

Все функции возвращают SQL-fragments (строки), которые встраиваются
в готовые запросы. Принципиально не используют параметризацию по
колонкам (колонки/таблицы не могут быть bind-параметрами в psycopg).

Поддерживаемые форматы дат:
  - ISO  'YYYY-MM-DD' и 'YYYY-MM-DD HH:MM:SS'
  - 'DD-MM-YYYY'      (legacy DLD csv export)
  - 'DD/MM/YYYY'      (DLD Pulse weekly dump)
  - epoch seconds (10 digits) — на всякий случай

Все функции NULL-safe. Невалидный вход → NULL, без exceptions.
"""

from __future__ import annotations

import re

# Разрешаем только идентификаторы, которые безопасно встраивать в SQL.
# Колонка может приходить в форме   table.col   или   "Schema"."Col"  ;
# для упрощения требуем буквы/цифры/подчёркивание/точку/двойные кавычки.
_IDENT_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_."]*$')


def _check_ident(col: str) -> str:
    """Защита от SQL-инъекции через имя колонки.
    Контракт-валидатор и боты передают имена колонок из своего
    кода, но мы всё равно перепроверяем."""
    if not col or not isinstance(col, str):
        raise ValueError(f"safe_coerce: invalid column name: {col!r}")
    if not _IDENT_RE.match(col):
        raise ValueError(
            f"safe_coerce: column name contains unsafe characters: {col!r}"
        )
    return col


def safe_date_sql(col: str) -> str:
    """Возвращает SQL-CASE, который приводит `col` к date.

    Безопасно работает с text/date/timestamp.
    Любой неузнанный формат → NULL.

    Пример::

        SELECT {safe_date_sql("instance_date")} AS d FROM dld_transactions_full
    """
    c = _check_ident(col)
    # \\ в Python даёт \ в SQL.
    # Двойные {{ }} нужны только в f-string; здесь обычная строка, оставляем одинарные.
    return (
        "CASE "
        f"WHEN {c} IS NULL THEN NULL "
        f"WHEN {c}::text ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}' THEN ({c}::text)::date "
        f"WHEN {c}::text ~ '^\\d{{2}}-\\d{{2}}-\\d{{4}}$' THEN to_date({c}::text, 'DD-MM-YYYY') "
        f"WHEN {c}::text ~ '^\\d{{2}}/\\d{{2}}/\\d{{4}}$' THEN to_date({c}::text, 'DD/MM/YYYY') "
        f"WHEN {c}::text ~ '^\\d{{10}}$' THEN to_timestamp({c}::text::bigint)::date "
        "ELSE NULL "
        "END"
    )


def safe_num_sql(col: str) -> str:
    """Numeric-safe coerce. Удаляет всё, кроме [0-9.], NULL если пусто."""
    c = _check_ident(col)
    return (
        "CASE "
        f"WHEN {c} IS NULL THEN NULL "
        f"ELSE NULLIF(regexp_replace({c}::text, '[^0-9.]', '', 'g'), '')::numeric "
        "END"
    )


def safe_int_sql(col: str) -> str:
    """Integer coerce через numeric (psycopg любит). FLOOR для дробных."""
    c = _check_ident(col)
    return (
        "CASE "
        f"WHEN {c} IS NULL THEN NULL "
        f"ELSE FLOOR(NULLIF(regexp_replace({c}::text, '[^0-9.\\-]', '', 'g'), '')::numeric)::bigint "
        "END"
    )


def safe_bool_sql(col: str) -> str:
    """Bool coerce: 'true'/'yes'/'1' → TRUE, 'false'/'no'/'0' → FALSE, иначе NULL."""
    c = _check_ident(col)
    return (
        "CASE "
        f"WHEN {c} IS NULL THEN NULL "
        f"WHEN lower({c}::text) IN ('true','t','yes','y','1') THEN TRUE "
        f"WHEN lower({c}::text) IN ('false','f','no','n','0') THEN FALSE "
        "ELSE NULL "
        "END"
    )


def safe_text_sql(col: str, fallback: str = "''") -> str:
    """Text-safe: NULL → fallback, обрезаем пробелы по краям."""
    c = _check_ident(col)
    # fallback должен быть валидным SQL-литералом, поэтому не санируем — это контролируется кодом.
    return f"COALESCE(NULLIF(BTRIM({c}::text), ''), {fallback})"


# Backward-compat алиасы, чтобы боты могли импортировать привычные имена.
date_safe = safe_date_sql
num_safe = safe_num_sql
int_safe = safe_int_sql
bool_safe = safe_bool_sql
text_safe = safe_text_sql


if __name__ == "__main__":
    # Маленький self-test без БД.
    samples = ["instance_date", "actual_worth", "rooms_en"]
    for s in samples:
        print(f"safe_date_sql({s!r}):\n  {safe_date_sql(s)}\n")
        print(f"safe_num_sql({s!r}):\n  {safe_num_sql(s)}\n")
    try:
        safe_date_sql("col; DROP TABLE users;--")
        print("FAIL: injection guard missing")
    except ValueError as e:
        print(f"OK: injection blocked -> {e}")
