"""Deterministic classification for evidence quotation spans."""

from __future__ import annotations

import re
import unicodedata


_DISALLOWED = re.compile(
    r"(?:有効(?:である|です)|適合(?:している|です)|と解される|所見として|"
    r"したがって|よって|ゆえに|〜はない|〜できる|〜が相当)",
    re.IGNORECASE,
)
_QUANTITY = re.compile(r"(?:\d[\d,]*\s*(?:件|回|日|%|月|年)|対象件数)")
_DATE = re.compile(r"(?:\d{4}年\d{1,2}月\d{1,2}日|\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{4}年\d{1,2}月)")
_IDENTIFIER = re.compile(r"(?:番号|ID|規程)[^。\n]{0,40}")
_OBSERVED = re.compile(r"(?:空欄|未記入|記録が残されている|実施した記録)")


def classify_span(text: str, *, max_chars: int) -> str:
    """Return an allowlisted kind, or ``unclassified`` (the fail-closed default)."""

    normalized = unicodedata.normalize("NFKC", text).replace("\u200b", "").replace("\ufeff", "")
    if not normalized or len(text) > max_chars or _DISALLOWED.search(normalized):
        return "unclassified"
    if _QUANTITY.search(normalized):
        return "quantity"
    if _DATE.search(normalized):
        return "date"
    if _IDENTIFIER.search(normalized):
        return "identifier"
    if _OBSERVED.search(normalized):
        return "observed_condition"
    if "記録" in normalized or "承認" in normalized:
        return "procedural_record"
    return "unclassified"
