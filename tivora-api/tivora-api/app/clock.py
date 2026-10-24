from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))


def utcnow() -> datetime:
    """Naive UTC, so SQLite and Postgres behave the same."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def now_ist() -> datetime:
    return datetime.now(IST)
