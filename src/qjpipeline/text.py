"""Small, dependency-free helpers used by every source parser."""

from __future__ import annotations

import html
import re
import unicodedata
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

_BREAK_TAGS = frozenset(
    [
        "p",
        "div",
        "br",
        "ul",
        "ol",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "table",
        "tr",
        "section",
        "article",
        "blockquote",
        "pre",
        "hr",
    ]
)


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in _BREAK_TAGS:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        elif tag in _BREAK_TAGS:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.out.append(data)


def html_to_text(value: str | None) -> str | None:
    """Plain text from an HTML fragment, keeping paragraphs and bullets."""
    if not value:
        return None
    parser = _Text()
    parser.feed(re.sub(r"<[^>]*$", "", value))  # drop a tag cut off at the end
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.out).splitlines())
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text or None


def clean(value: object) -> str | None:
    """Unescape entities and collapse whitespace; empty strings become None."""
    if value is None:
        return None
    text = " ".join(html.unescape(str(value)).split())
    return text or None


def fold(text: str | None) -> str:
    """Lowercase ASCII words for matching: accents, punctuation and case removed."""
    if not text:
        return ""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9+#]+", " ", ascii_text.lower()).split())


def parse_datetime(value: object) -> datetime | None:
    """Epoch (seconds or milliseconds), ISO 8601 or RFC 2822 to naive UTC.

    Returns None for anything unparseable; the quality rules decide whether a
    missing date is acceptable.
    """
    if value is None or value == "":
        return None
    try:
        if isinstance(value, int | float) or (isinstance(value, str) and value.isdigit()):
            seconds = float(value)
            if seconds > 1e11:  # milliseconds
                seconds /= 1000
            return datetime.fromtimestamp(seconds, UTC).replace(tzinfo=None)
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def positive_int(value: object) -> int | None:
    try:
        number = int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def salary_text(
    low: int | None, high: int | None, currency: str | None, period: str = "year"
) -> str | None:
    """Human-readable salary, e.g. "USD 90,000-120,000 / year"."""
    if low is None and high is None:
        return None
    prefix = f"{currency} " if currency else ""
    if low is not None and high is not None and low != high:
        return f"{prefix}{low:,}-{high:,} / {period}"
    return f"{prefix}{(low or high):,} / {period}"
