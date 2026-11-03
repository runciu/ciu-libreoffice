"""Reading numbers and dates out of cell values. Plain Python, no UNO.

Rules follow what Calc itself does, so a value means the same thing in a
formula, in SQL and in a copied sheet:
  - "10%" is 0.1, as in a Calc formula
  - "(120.00)" is -120, the accounting style for negatives
  - "£1,200.50" is 1200.5
  - "00123" stays text: a leading zero marks a code, not a number
"""

import datetime as dt
import math
import re

EPOCH = dt.date(1899, 12, 30)   # Calc's day zero
CURRENCY = "£$€¥₦₹"
_THOUSANDS = re.compile(r"^\d{1,3}(,\d{3})+(\.\d+)?$")
_PLAIN = re.compile(r"^(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2})?$")
_DMY = re.compile(r"^(\d{1,2})[/.](\d{1,2})[/.](\d{4})$")


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and not (
        isinstance(v, float) and math.isnan(v))


def to_number(v):
    """A float for numbers and for text that plainly holds one, else None."""
    if _is_num(v):
        return float(v)
    if not isinstance(v, str):
        return None
    t = v.strip().lstrip("'").replace("−", "-").replace(" ", " ").strip()
    if not t:
        return None
    negative = False
    if t.startswith("(") and t.endswith(")"):
        negative, t = True, t[1:-1].strip()
    percent = t.endswith("%")
    if percent:
        t = t[:-1].strip()
    for _ in range(2):          # sign and currency in either order: -£5, £-5
        if t and t[0] in "+-":
            negative ^= t[0] == "-"
            t = t[1:].strip()
        t = t.strip(CURRENCY).strip()
    if not t:
        return None
    if re.match(r"^0\d", t):
        return None             # an identifier such as 00123 or 007
    if "," in t:
        if not _THOUSANDS.match(t):
            return None         # "1,5" could mean 1.5 or 15; do not guess
        t = t.replace(",", "")
    if not _PLAIN.match(t):
        return None
    if percent:
        # Shift the exponent instead of dividing, so "4.68%" is 0.0468 exactly
        # rather than 0.046799999999999994.
        f = float(t + "e-2") if "e" not in t.lower() else float(t) / 100.0
    else:
        f = float(t)
    if not math.isfinite(f):
        return None
    return -f if negative else f


def is_percent_text(v):
    return isinstance(v, str) and v.strip().lstrip("'").rstrip().endswith("%") and \
        to_number(v) is not None


def is_iso_date(v):
    return isinstance(v, str) and bool(_ISO_DATE.match(v))


def serial_to_iso(serial, kind="date"):
    """Calc day number to text: 2026-10-02, 2026-10-02 14:30, or 14:30."""
    days = math.floor(serial)
    minutes = int(round((serial - days) * 24 * 60))
    if minutes == 24 * 60:
        days, minutes = days + 1, 0
    date = EPOCH + dt.timedelta(days=days)
    clock = "%02d:%02d" % divmod(minutes, 60)
    if kind == "time":
        return clock
    if kind == "datetime":
        return "%s %s" % (date.isoformat(), clock)
    return date.isoformat()


def iso_to_serial(text):
    """Inverse of serial_to_iso for dates and date-times."""
    date = dt.date.fromisoformat(text[:10])
    serial = float((date - EPOCH).days)
    if len(text) > 10:
        h, m = text[11:16].split(":")
        serial += (int(h) * 60 + int(m)) / (24.0 * 60)
    return serial


_NAMED = ("%Y-%m-%d", "%Y/%m/%d", "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y",
          "%b %d, %Y", "%B %d, %Y", "%d-%b-%Y", "%d %b, %Y")


def parse_date(text, order=None):
    """ISO date for text holding one, else None. Day/month order for numeric
    dates comes from `order` ("dmy" or "mdy"), or from the value itself when
    one part is above 12; otherwise the date is ambiguous and not parsed."""
    if not isinstance(text, str):
        return None
    t = text.strip().lstrip("'").strip()
    if not t:
        return None
    for fmt in _NAMED:
        try:
            return dt.datetime.strptime(t, fmt).date().isoformat()
        except ValueError:
            pass
    m = _DMY.match(t)
    if not m:
        return None
    a, b, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if order is None:
        order = "dmy" if a > 12 else "mdy" if b > 12 else None
    if order is None:
        return None
    day, month = (a, b) if order == "dmy" else (b, a)
    try:
        return dt.date(year, month, day).isoformat()
    except ValueError:
        return None


def date_order(values):
    """Day/month order shared by a column's numeric dates: "dmy", "mdy", or
    None when nothing decides it or the column contradicts itself."""
    seen = set()
    for v in values:
        m = _DMY.match(v.strip().lstrip("'").strip()) if isinstance(v, str) else None
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > 12:
                seen.add("dmy")
            elif b > 12:
                seen.add("mdy")
    return seen.pop() if len(seen) == 1 else None


DATE_KINDS = ("date", "datetime", "time")


def apply_kinds(rows, kinds, min_share=0.6):
    """Rows rewritten so each value matches how Calc shows it, the final
    column kinds, and {column: [row indexes]} of dates that were typed as
    text (worth reporting, since such dates break Calc formulas). Day numbers in date columns become ISO text, TRUE/
    FALSE cells become those words, and a column whose text mostly reads as
    dates becomes a date column. Text that cannot be read is left alone."""
    kinds = dict(kinds)
    width = max((len(r) for r in rows), default=0)
    out = [list(r) for r in rows]
    typed = {}
    for c in range(width):
        cells = [r[c] for r in out if c < len(r)]
        texts = [v for v in cells if isinstance(v, str) and v.strip()]
        order = date_order(texts)
        if c not in kinds and texts:
            parsed = sum(1 for v in texts if parse_date(v, order))
            numbers = sum(1 for v in cells if _is_num(v))
            if parsed >= 2 and parsed >= min_share * (len(texts) + numbers):
                kinds[c] = "date"
        kind = kinds.get(c)
        if kind is None:
            continue
        for n, r in enumerate(out):
            if c >= len(r):
                continue
            v = r[c]
            if kind in DATE_KINDS:
                if _is_num(v):
                    r[c] = serial_to_iso(float(v), kind)
                elif isinstance(v, str):
                    iso = parse_date(v, order)
                    if iso:
                        r[c] = iso
                        typed.setdefault(c, []).append(n)
            elif kind == "logical" and _is_num(v):
                r[c] = "TRUE" if v else "FALSE"
    return out, kinds, typed
