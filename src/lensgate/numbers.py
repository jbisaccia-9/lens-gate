"""Numbers in prose, and where they came from.

L5's contract: the warehouse computes, the model transports. Every number the
person reads must equal a value in a result this turn actually returned (or a
number they typed themselves). "Equal" is exact decimal equality after
stripping thousands separators, a currency sign and a percent sign - so
412,330.50 matches 412330.5, but 412,331 does not, and neither does 4.2%
when the result says 4.17. A number with letters glued on ("1.79M", "3x",
"5th") is never equal to anything: abbreviating is rounding with extra steps.
"""
from __future__ import annotations

import re
from decimal import Decimal

# A number not preceded by a word character or a decimal point (so "Q3" and
# "v2" are labels, not numbers), with optional minus, $, thousands separators,
# decimals, %, and any letters glued to the end.
_NUM = re.compile(r"(?<![\w.])(-?)\$?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(%?)([A-Za-z]*)")


def extract(text: str) -> list[tuple[str, Decimal | None]]:
    """(as written, value) for each number; value is None when letters are glued on."""
    out = []
    for m in _NUM.finditer(text):
        value = None if m.group(5) else Decimal(m.group(1) + m.group(2).replace(",", "") + (m.group(3) or ""))
        out.append((m.group(0), value))
    return out


def sources(values) -> set[Decimal]:
    """Every number a set of result cells (or a question) makes available."""
    found: set[Decimal] = set()
    for v in values:
        if v is None or isinstance(v, bool):
            continue
        if isinstance(v, (int, float, Decimal)):
            found.add(Decimal(str(v)).normalize())  # signed: -3.1 must be written -3.1
        else:
            found.update(d.normalize() for _, d in extract(str(v)) if d is not None)
    return found


def unsourced(answer: str, available: set[Decimal]) -> list[str]:
    return [raw for raw, d in extract(answer) if d is None or d.normalize() not in available]
