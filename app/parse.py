"""Convert raw export values, following the formats in data/README.md."""

from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

ROME = ZoneInfo("Europe/Rome")


def blank(value):
    # Empty means unknown. Turning it into NULL lets NOT NULL columns reject missing required values.
    return value if value != "" else None


def decimal_comma(value):
    return Decimal(value.replace(",", ".")) if value else None


def dmy_date(value):
    return datetime.strptime(value, "%d/%m/%Y").date() if value else None


def dmy_datetime_rome(value):
    # Inside a clock change, fold=0 applies the UTC offset in force before the change.
    return datetime.strptime(value, "%d/%m/%Y %H:%M").replace(tzinfo=ROME) if value else None


def status(value):
    return value.strip().lower() or None


def completion(value):
    return {"Y": True, "N": False, "": None}[value]


if __name__ == "__main__":
    assert blank("") is None and blank("x") == "x"
    assert decimal_comma("12500,00") == Decimal("12500.00") and decimal_comma("") is None
    assert dmy_date("24/06/2027").isoformat() == "2027-06-24" and dmy_date("") is None
    assert status(" OPEN ") == "open" and status("Won") == "won"
    assert completion("Y") is True and completion("N") is False and completion("") is None
    utc = lambda v: dmy_datetime_rome(v).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
    assert utc("25/08/2026 10:30") == "2026-08-25 08:30"  # summer time, +02:00
    assert utc("30/03/2025 02:30") == "2025-03-30 01:30"  # never existed: offset before the change
    assert utc("26/10/2025 02:19") == "2025-10-26 00:19"  # happened twice: first occurrence
    print("parse: ok")
