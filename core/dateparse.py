from datetime import datetime

_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%y")


def parse_date(date_str: str) -> datetime | None:
    """Parse date string in common bank formats. Returns None if unparseable."""
    for fmt in _FORMATS:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None


def parse_date_to_iso(date_str: str) -> str | None:
    """Parse date string and return ISO YYYY-MM-DD. Returns None if unparseable."""
    dt = parse_date(date_str)
    return dt.strftime("%Y-%m-%d") if dt else None
