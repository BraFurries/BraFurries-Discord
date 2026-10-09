from datetime import datetime, date
import re
from typing import Iterable

SUPPORTED_DATE_FORMATS: tuple[str, ...] = (
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%d/%m/%y",
    "%d-%m-%y",
    "%d.%m.%y",
)

PORTUGUESE_MONTHS: dict[str, int] = {
    "janeiro": 1,
    "fevereiro": 2,
    "março": 3,
    "marco": 3,
    "abril": 4,
    "maio": 5,
    "junho": 6,
    "julho": 7,
    "agosto": 8,
    "setembro": 9,
    "outubro": 10,
    "novembro": 11,
    "dezembro": 12,
}

PORTUGUESE_MONTHS_ABBR: dict[str, int] = {
    "jan": 1,
    "fev": 2,
    "mar": 3,
    "abr": 4,
    "mai": 5,
    "jun": 6,
    "jul": 7,
    "ago": 8,
    "set": 9,
    "out": 10,
    "nov": 11,
    "dez": 12,
}

LONG_DATE_PATTERN = re.compile(
    r"^\s*(\d{1,2})(?:º|°)?\s+de\s+([A-Za-zÀ-ÿ]+)\s+de\s+(\d{2}|\d{4})(?:[.,])?\s*$",
    re.IGNORECASE,
)
ABBREVIATED_MONTH_DATE_PATTERN = re.compile(
    r"^\s*(\d{1,2})\s*[/.-]\s*([A-Za-zÀ-ÿ]{3})\s*[/.-]\s*(\d{2}|\d{4})\s*$",
    re.IGNORECASE,
)

TEXT_DATE_LONG_PATTERN = re.compile(
    r"\b(\d{1,2}(?:º|°)?\s+de\s+[A-Za-zÀ-ÿ]+\s+de\s+\d{2,4})(?:[.,])?\b",
    re.IGNORECASE,
)
TEXT_DATE_COMPACT_PATTERN = re.compile(r"\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b")
TEXT_DATE_ABBR_PATTERN = re.compile(
    r"\b\d{1,2}[/.-][A-Za-zÀ-ÿ]{3}[/.-]\d{2,4}\b",
    re.IGNORECASE,
)
SPACED_NUMERIC_DATE_PATTERN = re.compile(
    r"^\s*(\d(?:\s?\d)?)\s*([/.-])\s*(\d(?:\s?\d)?)\s*\2\s*(\d(?:\s?\d){1,3})\s*$"
)


def _iter_date_formats(dateFormat: str | None) -> Iterable[str]:
    if dateFormat is not None:
        return (dateFormat,)
    return SUPPORTED_DATE_FORMATS


def verifyDate(date_str: str, dateFormat: str | None = None) -> date | bool:
    """Return a ``datetime.date`` if the string matches any supported format or ``False``."""

    if isinstance(date_str, str):
        spaced_numeric_match = SPACED_NUMERIC_DATE_PATTERN.match(date_str)
        if spaced_numeric_match:
            day_str, separator, month_str, year_str = spaced_numeric_match.groups()
            if " " not in day_str and " " not in month_str and " " not in year_str:
                spaced_numeric_match = None
            else:
                normalized_date = (
                    f"{day_str.replace(' ', '')}{separator}"
                    f"{month_str.replace(' ', '')}{separator}"
                    f"{year_str.replace(' ', '')}"
                )
                normalized_result = verifyDate(normalized_date, dateFormat)
                if normalized_result:
                    return normalized_result

        long_date_match = LONG_DATE_PATTERN.match(date_str)
        if long_date_match:
            day_str, month_name, year_str = long_date_match.groups()
            month_number = PORTUGUESE_MONTHS.get(month_name.casefold())
            if month_number is None:
                return False

            try:
                day = int(day_str)
                year = int(year_str)
                if len(year_str) == 2:
                    year += 2000 if year <= 68 else 1900
                return date(year, month_number, day)
            except ValueError:
                return False

        abbreviated_month_match = ABBREVIATED_MONTH_DATE_PATTERN.match(date_str)
        if abbreviated_month_match:
            day_str, month_abbr, year_str = abbreviated_month_match.groups()
            month_number = PORTUGUESE_MONTHS_ABBR.get(month_abbr.casefold())
            if month_number is None:
                return False

            try:
                day = int(day_str)
                year = int(year_str)
                if len(year_str) == 2:
                    year += 2000 if year <= 68 else 1900
                return date(year, month_number, day)
            except ValueError:
                return False

    for fmt in _iter_date_formats(dateFormat):
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    return False


def extract_date_from_text(text: str) -> date | bool:
    """Extract and validate the first supported date found in ``text`` order."""

    if not isinstance(text, str):
        return False

    direct_match = verifyDate(text.strip())
    if direct_match:
        return direct_match

    candidate_matches: list[tuple[int, str]] = []
    for pattern in (
        TEXT_DATE_COMPACT_PATTERN,
        TEXT_DATE_LONG_PATTERN,
        TEXT_DATE_ABBR_PATTERN,
    ):
        for match in pattern.finditer(text):
            candidate_matches.append((match.start(), match.group(1) if match.lastindex else match.group(0)))

    candidate_matches.sort(key=lambda item: item[0])
    for _, candidate in candidate_matches:
        parsed_date = verifyDate(candidate)
        if parsed_date:
            return parsed_date

    return False


def validate_birthdate(birthday: date) -> None:
    """Ensure ``birthday`` is a plausible past date.

    Raises
    ------
    ValueError
        If ``birthday`` is in the future or before 1900-01-01.
    TypeError
        If ``birthday`` is not a :class:`datetime.date` instance.
    """

    if not isinstance(birthday, date):
        raise TypeError("Birthday must be a date object")

    if birthday > datetime.now().date():
        raise ValueError("Birth date cannot be in the future")

    if birthday < date(1900, 1, 1):
        raise ValueError("Birth date out of allowed range")
