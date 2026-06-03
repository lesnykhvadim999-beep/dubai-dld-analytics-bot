"""Rate limit: max 1 follow-up / user / 48ч + global opt-out."""
import os
from ._db import last_followup_age_hours, optout, is_opted_out_db

MIN_HOURS_BETWEEN = float(os.getenv("CR_MIN_HOURS_BETWEEN", "48"))


def is_opted_out(user_id: int) -> bool:
    return is_opted_out_db(user_id)


def can_send_followup(user_id: int) -> bool:
    if is_opted_out(user_id):
        return False
    age = last_followup_age_hours(user_id)
    if age is None:
        return True
    return age >= MIN_HOURS_BETWEEN


def mark_optout(user_id: int, bot: str = "") -> bool:
    return optout(user_id, bot or None)
