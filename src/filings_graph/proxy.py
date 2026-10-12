"""The beneficial-ownership tables of a DEF 14A (Reg S-K Item 403): holders more than 5% as the
company itself reports them, and the directors-and-officers table.

Deterministic: every `<table>` whose header cells name a percent column (percent of class, % of
class, percent of outstanding, percentage) and a holder column (name, beneficial owner) is read
row by row; a row keeps its first text cell as the name and the percent cell as `pct` (`*` and
`less than 1%` read as under 1). The table is classed `holders` when its header mentions an
address (the 5% table says "Name and Address of Beneficial Owner") and `insiders` otherwise.
Parsing has known misses; the tests pin the cases found so far.
"""

from __future__ import annotations

import html as htmlmod
import re
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any

_TABLE = re.compile(r"<table\b.*?</table>", re.DOTALL | re.IGNORECASE)
_ROW = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_CELL = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.DOTALL | re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_PCT = re.compile(r"^(\d{1,3}(?:\.\d+)?)\s*%?$")  # no parentheses: "(5)" is a footnote marker, not 5%
_STREET = re.compile(
    r"\s+(?:\d{1,6}|One|Two|Three|Four|Five|Six|Seven|Eight|Nine|Ten)\s+\S+.*?(?:Street|St\.|Avenue|Ave\.?|Road|Rd\.?|Blvd\.?|Boulevard|Plaza|Square|Way|Drive|Dr\.?|Place|Park|Center|Centre|Lane|Yards|Parkway|Floor|Suite)\b.*$",
    re.IGNORECASE,
)
_PCT_WORD = re.compile(r"percent(?:age)?|%\s*(?:of\b|class\b|shares?\b|outstanding\b|$)", re.IGNORECASE)
_NAME_WORD = re.compile(r"name|beneficial owner|beneficially owned|holder", re.IGNORECASE)
_OWN_WORD = re.compile(
    r"beneficial|ownership|shares|stock|owned", re.IGNORECASE
)  # a pay table also has Name and %
_ADDRESS = re.compile(r"address", re.IGNORECASE)
_SUPER = re.compile(r"[⁰¹²³⁴⁵⁶⁷⁸⁹*†‡§]+$")  # superscript footnote markers trailing a cell
_TRAILING_BRACKETED_MARKER = re.compile(
    r"(?:\s*,\s*|\s*)(?:\(\s*([^()]*)\s*\)|\[\s*([^\[\]]*)\s*\])\s*$",
    re.IGNORECASE,
)
_BRACKETED_MARKER_CONTENT = re.compile(r"(?:[ivxlcdm]+|\d+|[a-z])", re.IGNORECASE)
_CLASS_HEADER = re.compile(
    r"^(?:title\s+of\s+class|class(?:\s+of\s+(?:stock|securities))?|"
    r"title\s+or\s+class\s+of\s+securities)(?:\s*\([^)]*\))?$",
    re.IGNORECASE,
)
_CLASS_WORD = re.compile(
    r"\b(?:common\s+stock|preferred\s+stock|ordinary\s+shares|common|stock)\b", re.IGNORECASE
)
_LEGAL_OR_PERSON_WORD = re.compile(
    r"\b(?:incorporated|inc\.?|corporation|corp\.?|company|co\.?|llc|llp|l\.?p\.?|limited|ltd\.?|plc|"
    r"trust|fund|partners?|group|holdings?|bank|management|capital|advisers?|investors?)\b",
    re.IGNORECASE,
)
_OWNER_SECTION = re.compile(
    r"\b(?:5\s*%\s*(?:shareholders?|stockholders?)|(?:greater|more)\s+than\s+5\s*%|"
    r"principal\s+(?:stockholders?|shareholders?)|certain\s+beneficial\s+owners?)\b",
    re.IGNORECASE,
)
_CONNECTOR_END = re.compile(r"\b(?:with|of|by|for|and)$", re.IGNORECASE)


@dataclass(frozen=True)
class OwnershipRow:
    table: str  # holders | insiders
    name: str
    pct: float | None  # None when the cell says * / less than 1%
    shares: int | float | None
    raw: str
    class_title: str | None = None
    suspect: bool = False
    suspect_reason: str | None = None
    proxy_table_conflict: dict[str, Any] | None = None
    source_table_index: int = 0


def _text(fragment: str) -> str:
    visible = htmlmod.unescape(_TAG.sub(" ", fragment)).replace("\xa0", " ")
    visible = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", visible)
    return re.sub(r"\s+", " ", visible).strip()


def _cells(row: str) -> list[str]:
    cells = [_text(c) for c in _CELL.findall(row)]
    # inline XBRL and layout tables split "1.06" and "%" into two cells; join them back
    out: list[str] = []
    for c in cells:
        if c == "%" and out and _PCT.match(out[-1]):
            out[-1] = out[-1] + "%"
        elif c:
            out.append(c)
    return out


def _shares(cells: list[str]) -> int | float | None:
    for index, c in enumerate(cells):
        if "$" in c or (index and cells[index - 1].strip() == "$"):
            continue
        s = _SUPER.sub("", c.replace(",", ""))
        if "%" in s:
            continue
        m = re.fullmatch(
            r"\s*(\d+(?:\.\d+)?)(?:\s*(?:\(\s*\d{1,2}\s*\)|\[\s*\d{1,2}\s*\]|\s+\d{1,2}))*\s*",
            s,
        )
        if not m:
            continue
        try:
            value = Decimal(m.group(1))
        except InvalidOperation:
            continue
        # Keep integer-like decimal disclosures such as "1.00" without confusing an unformatted
        # one-digit percentage ("5") in the next column for a share count.
        if value == value.to_integral_value() and ("." in m.group(1) or len(m.group(1)) >= 3):
            return int(value)
        if "." in m.group(1):
            return float(value)
    return None


def _first_line(fragment: str) -> str:
    """Read a holder name before its address, joining a connector-led continuation line."""
    lines = re.split(r"<br\b[^>]*>|</p\s*>|</div\s*>", fragment, flags=re.IGNORECASE)
    visible_lines = [visible for line in lines if (visible := _text(line))]
    if not visible_lines:
        return ""
    name = visible_lines[0]
    for line in visible_lines[1:]:
        if not _CONNECTOR_END.search(name):
            break
        name = f"{name} {line}"
    return name


def _is_class_column(header_rows: list[list[str]]) -> bool:
    return any(
        len(row) >= 2
        and _CLASS_HEADER.fullmatch(row[0].strip())
        and re.search(r"\b(?:name|owner|holder)\b", " ".join(row[1:]), re.IGNORECASE)
        for row in header_rows
    )


def _is_class_title(value: str, explicit_column: bool) -> bool:
    if explicit_column:
        return True
    value = _strip_trailing_bracketed_markers(value).strip(" ,;*†‡§")
    if not _CLASS_WORD.search(value):
        return False
    if re.search(r"\bstock\s+options?\b|\bshares?\s+earned\b", value, re.IGNORECASE):
        return False
    # A company or holder name can contain "stock"; only the unmistakable class-only forms
    # are inferred without an explicit class-column heading.
    words = re.sub(r"\bclass\s+[a-z0-9]+\b", "", value, flags=re.IGNORECASE).strip()
    return not _LEGAL_OR_PERSON_WORD.search(words)


def _strip_trailing_bracketed_markers(name: str) -> str:
    """Remove terminal proxy footnotes, preserving unbracketed numerals in holder names."""
    value = name
    while match := _TRAILING_BRACKETED_MARKER.search(value):
        marker = (match.group(1) if match.group(1) is not None else match.group(2) or "").strip()
        if not _BRACKETED_MARKER_CONTENT.fullmatch(marker):
            break
        value = value[: match.start()].rstrip(" ,")
    return value


_ZIP = re.compile(r"\b\d{5}(?:-\d{4})?\b")
_STREET_HEAD = re.compile(
    r"^\s*\d{1,6}\s+[A-Za-z0-9.'-]+(?:\s+[A-Za-z0-9.'-]+){0,5}\s+"
    r"(?:Street|St\.?|Avenue|Ave\.?|Road|Rd\.?|Blvd\.?|Boulevard|Lane|Ln\.?|"
    r"Drive|Dr\.?|Way|Place|Plaza|Parkway|Pkwy\.?|Suite|Floor)\b",
    re.IGNORECASE,
)


def _item_403_rejection_reason(header: str, rows: list[OwnershipRow]) -> str | None:
    ownership = re.search(
        r"\bbeneficial\s+(?:ownership|owner)\b|\bbeneficially\s+owned\b|"
        r"\bownership\s+of\s+(?:the\s+)?(?:common\s+)?(?:stock|shares?)\b|"
        r"\bshares?\s+owned\b",
        header,
        re.IGNORECASE,
    )
    percent_column = _PCT_WORD.search(header)
    explicit_class_percent = percent_column and re.search(r"\bclass\b", header, re.IGNORECASE)
    named_share_table = bool(
        re.search(r"\bname\b", header, re.IGNORECASE)
        and re.search(r"\b(?:number\s+of\s+)?shares?\b", header, re.IGNORECASE)
        and _PCT_WORD.search(header)
    )
    section_share_table = bool(
        _OWNER_SECTION.search(header)
        and re.search(r"\b(?:number\s+of\s+)?shares?\b|\bnumber\b", header, re.IGNORECASE)
        and percent_column
    )
    if not percent_column or not (
        ownership or explicit_class_percent or named_share_table or section_share_table
    ):
        return "header does not identify beneficial ownership and percent of a class or outstanding shares"
    if any(row.pct is not None and row.pct > 100 for row in rows):
        return "ownership percentage exceeds 100"
    if not any(row.shares is not None for row in rows) and not (
        ownership or named_share_table or section_share_table
    ):
        return "rows do not report beneficially owned shares"
    return None


def _flag_suspect_rows(rows: list[OwnershipRow]) -> list[OwnershipRow]:
    implied_by_class: dict[str, list[tuple[OwnershipRow, float]]] = {}
    for row in rows:
        if row.pct is not None and row.pct >= 1 and row.shares is not None and row.shares > 0:
            class_key = (row.class_title or "").casefold().strip()
            implied_by_class.setdefault(class_key, []).append((row, row.shares / (row.pct / 100)))
    results = []
    for row in rows:
        key = (row.class_title or "").casefold().strip()
        implied = next(
            (value for candidate, value in implied_by_class.get(key, []) if candidate is row), None
        )
        values = [value for _, value in implied_by_class.get(key, [])]
        if implied is None or not values:
            results.append(row)
            continue
        center = median(values)
        deviation = abs(implied - center) / center if center else 0
        if row.pct is not None and row.pct >= 1 and deviation > 0.25:
            results.append(
                replace(
                    row,
                    suspect=True,
                    suspect_reason=(
                        f"implied shares outstanding {implied:.0f} differs from table/class median "
                        f"{center:.0f} by {deviation:.1%}"
                    ),
                )
            )
        else:
            results.append(row)
    qualifying_shares = [
        row.shares
        for row in results
        if row.pct is not None and row.pct >= 5.0 and row.shares is not None and row.shares > 0
    ]
    if qualifying_shares:
        smallest_qualifying = min(qualifying_shares)
        results = [
            replace(
                row,
                suspect=True,
                suspect_reason="percent inconsistent with shares in this table",
            )
            if row.shares is not None
            and row.shares >= smallest_qualifying
            and row.pct is not None
            and row.pct < 5.0
            else row
            for row in results
        ]
    return results


def _coalesce_rows(rows: list[OwnershipRow]) -> list[OwnershipRow]:
    groups: dict[tuple[str, str], list[OwnershipRow]] = {}
    for row in rows:
        name_key = re.sub(r"[^a-z0-9]+", " ", row.name.casefold()).strip()
        class_key = re.sub(r"[^a-z0-9]+", " ", (row.class_title or "").casefold()).strip()
        groups.setdefault((name_key, class_key), []).append(row)
    result = []
    for duplicates in groups.values():
        table_ids = {row.source_table_index for row in duplicates}
        if len(table_ids) == 1:
            seen: set[tuple[int | float | None, float | None]] = set()
            for row in duplicates:
                identity = (row.shares, row.pct)
                if identity not in seen:
                    result.append(row)
                    seen.add(identity)
            continue
        preferred_table = next(
            (row.source_table_index for row in duplicates if row.table == "holders"),
            duplicates[0].source_table_index,
        )
        preferred_rows = [row for row in duplicates if row.source_table_index == preferred_table]
        other = next((row for row in duplicates if row.source_table_index != preferred_table), None)
        if (
            other
            and preferred_rows
            and preferred_rows[0].pct is not None
            and other.pct is not None
            and abs(preferred_rows[0].pct - other.pct) > 0.05
        ):
            preferred_rows[0] = replace(
                preferred_rows[0],
                proxy_table_conflict={
                    "percentages": [preferred_rows[0].pct, other.pct],
                    "raw_cells": [preferred_rows[0].raw, other.raw],
                },
            )
        result.extend(preferred_rows)
    return result


def parse_ownership_tables_audited(
    raw_html: str,
    audit_counts: dict[str, int] | None = None,
) -> tuple[list[OwnershipRow], list[dict[str, str]], list[dict[str, Any]]]:
    if audit_counts is not None:
        audit_counts.setdefault("tables_with_candidate_row", 0)
        audit_counts.setdefault("tables_with_candidate_row_neither_outcome", 0)
    rows: list[OwnershipRow] = []
    rejected: list[dict[str, str]] = []
    rejected_tables: list[dict[str, Any]] = []
    previous_table_end = 0
    for table_index, table_match in enumerate(_TABLE.finditer(raw_html)):
        table = table_match.group(0)
        trs = _ROW.findall(table)
        if len(trs) < 2:
            previous_table_end = table_match.end()
            continue
        head_rows = []
        # SEC tables often put title and spacer rows before a multi-row header. Keep scanning
        # those rows until the first share/percentage-shaped data row establishes the boundary.
        for tr in trs[:30]:
            cells = _cells(tr)
            numeric_percent = any(
                (match := _PCT.match(c.replace(",", "")))
                and (c.endswith("%") or float(match.group(1)) <= 100)
                for c in cells
            )
            header_start = bool(
                cells
                and re.match(
                    r"(?i)^(?:name\b|beneficial owner\b|owner\b|shares?\b|percent(?:age)?\b|class\b)",
                    cells[0],
                )
            )
            if len(cells) >= 2 and numeric_percent and not header_start and _shares(cells[1:]) is not None:
                break
            head_rows.append(cells)
        header = " ".join(" ".join(c) for c in head_rows)
        header_table_signal = bool(
            _PCT_WORD.search(header) and _NAME_WORD.search(header) and _OWN_WORD.search(header)
        )
        ownership_header = bool(
            re.search(r"beneficial\s+(?:ownership|owner)|beneficially\s+owned", header, re.IGNORECASE)
        )
        share_count_header = bool(
            re.search(
                r"\b(?:number\s+of\s+)?shares?\b|\bshares?\s+beneficially\s+owned\b", header, re.IGNORECASE
            )
            or (ownership_header and re.search(r"\bamount\s+and\s+nature\b", header, re.IGNORECASE))
            or (_OWNER_SECTION.search(header) and re.search(r"\bnumber\b", header, re.IGNORECASE))
        )
        five_percent_heading = re.search(
            r"(?:5\s*%|five\s+percent).{0,40}(?:stockholder|shareholder|beneficial owner)|"
            r"(?:stockholder|shareholder|beneficial owner).{0,40}(?:5\s*%|five\s+percent)|"
            r"persons? known to own.{0,30}five\s+percent",
            header,
            re.IGNORECASE,
        )
        nearby = _text(raw_html[max(previous_table_end, table_match.start() - 1800) : table_match.start()])
        five_percent_context = re.search(
            r"(?:beneficial owners? (?:known to (?:the )?company )?(?:of more than|over|above)\s*5\s*%|"
            r"known to (?:the )?company to be the beneficial owners? of more than\s*5\s*%|"
            r"security ownership of certain beneficial owners?|5\s*% (?:holders|stockholders|shareholders))",
            nearby,
            re.IGNORECASE,
        )
        owner_section = _OWNER_SECTION.search(header) or _OWNER_SECTION.search(nearby)
        kind = (
            "holders"
            if _ADDRESS.search(header) or five_percent_heading or five_percent_context or owner_section
            else "insiders"
        )
        table_rows: list[OwnershipRow] = []
        table_rejected: list[dict[str, str]] = []
        # the percent column, by header position, for tables whose cells carry no "%" sign
        pct_cols: list[tuple[int, int]] = []
        for hr in head_rows:
            for i, c in enumerate(hr):
                if _PCT_WORD.search(c) or c.strip() == "%":
                    pct_cols.append((len(hr), i))
        explicit_class_column = _is_class_column(head_rows)
        for tr in trs:
            cell_fragments = [fragment for fragment in _CELL.findall(tr) if _text(fragment)]
            cells = _cells(tr)
            if len(cells) < 2:
                continue
            class_title = None
            original_cells = list(cells)
            if _is_class_title(cells[0], explicit_class_column) and len(cells) >= 3:
                class_title = _strip_trailing_bracketed_markers(cells[0]).strip(" ,;")
                cells = cells[1:]  # the first column names the class; the holder is next
                cell_fragments = cell_fragments[1:]
            name = _first_line(cell_fragments[0]) if cell_fragments else cells[0]
            name = _strip_trailing_bracketed_markers(_SUPER.sub("", name)).strip(" ,;*†‡§")
            if not name or _PCT_WORD.search(name) or _NAME_WORD.search(name) or len(name) < 3:
                continue
            pct: float | None = None
            found = False
            for c in cells[1:]:
                pct_cell = _strip_trailing_bracketed_markers(_SUPER.sub("", c.replace(",", ""))).strip()
                m = _PCT.match(pct_cell)
                if pct_cell.endswith("%") and m:
                    pct, found = float(m.group(1)), True
                    break
                if pct_cell in ("*", "**") or re.match(r"(?i)less than 1|<\s*1", pct_cell):
                    pct, found = None, True
                    break
            if not found:
                for width, column in pct_cols:
                    if class_title:
                        width -= 1
                        column -= 1
                    if len(cells) != width or column >= len(cells):
                        continue
                    pct_cell = _strip_trailing_bracketed_markers(
                        _SUPER.sub("", cells[column].replace(",", ""))
                    ).strip()
                    m = _PCT.match(pct_cell)
                    if m and float(m.group(1)) <= 100:
                        pct, found = float(m.group(1)), True
                        break
            if not found:
                continue
            raw_name = name
            name = _STREET.sub("", name).strip(" ,;")  # cut a street address after an inline holder name
            candidate = name or raw_name
            if _ZIP.search(candidate):
                table_rejected.append(
                    {"name": candidate, "reason": "name contains ZIP code", "raw": " | ".join(cells)[:300]}
                )
                continue
            if _STREET_HEAD.search(candidate):
                table_rejected.append(
                    {
                        "name": candidate,
                        "reason": "name begins with street address",
                        "raw": " | ".join(cells)[:300],
                    }
                )
                continue
            if not name:
                continue
            if name.lower().startswith(
                ("all directors", "all executive", "all current", "directors and executive")
            ):
                continue
            table_rows.append(
                OwnershipRow(
                    table=kind,
                    name=name,
                    pct=pct,
                    shares=_shares(cells[1:]),
                    raw=" | ".join(original_cells)[:300],
                    class_title=class_title,
                    source_table_index=table_index,
                )
            )
        rejection_reason = _item_403_rejection_reason(f"{header} {nearby}", table_rows)
        candidate_rows = [row for row in table_rows if row.shares is not None and row.pct is not None]
        non_share_payout = bool(
            re.search(r"\b(?:target|actual)\s+payout\s+opportunity\s*\(\s*\$\s*\)", header, re.IGNORECASE)
            and not share_count_header
        )
        if candidate_rows and audit_counts is not None:
            audit_counts["tables_with_candidate_row"] = audit_counts.get("tables_with_candidate_row", 0) + 1
        has_candidate_outcome = False
        if rejection_reason:
            if candidate_rows or (table_rows and header_table_signal and not non_share_payout):
                rejected_tables.append(
                    {"header": header[:2000], "reason": rejection_reason, "rows": len(table_rows)}
                )
                has_candidate_outcome = bool(candidate_rows)
        else:
            rows.extend(_flag_suspect_rows(table_rows))
            rejected.extend(table_rejected)
            has_candidate_outcome = bool(candidate_rows and table_rows)
        if candidate_rows and audit_counts is not None and not has_candidate_outcome:
            audit_counts["tables_with_candidate_row_neither_outcome"] += 1
        previous_table_end = table_match.end()
    return _coalesce_rows(rows), rejected, rejected_tables


def parse_ownership_tables_detailed(raw_html: str) -> tuple[list[OwnershipRow], list[dict[str, str]]]:
    rows, rejected, _ = parse_ownership_tables_audited(raw_html)
    return rows, rejected


def parse_ownership_tables(raw_html: str) -> list[OwnershipRow]:
    """Compatibility wrapper that returns only accepted table rows."""
    return parse_ownership_tables_detailed(raw_html)[0]


def five_percent_holders(rows: list[OwnershipRow]) -> list[OwnershipRow]:
    return [r for r in rows if r.pct is not None and r.pct >= 5.0]
