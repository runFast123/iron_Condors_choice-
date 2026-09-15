"""Every table's header count must match the cells in its rows.

A column added to a header without its cell shifts every value one place left,
silently. It happened on the payoff page: "Max profit" showed the loss as a
negative number, "Max loss" showed a pair of strike prices, and "Breakeven(s)"
was blank. A user sizing a position off that table reads the loss as the
profit, and nothing complains -- TypeScript cannot see it, the build succeeds,
and the page renders.

    python web/check_tables.py

A script rather than a component test, because the rule is structural and
applies to every table in the app, including the ones nobody wrote a test for.

Scans by tracking nesting depth rather than by matching pairs of tags. These
blotters put a whole table inside a row of another one, and every regex
approach to that quietly attributed the inner table's rows to the outer
table's headers -- reporting a fault in code that was correct, which is the way
to get a check ignored.

Tables whose columns are generated (`.map(` between the header tags) are
skipped: their width is a runtime value, so there is no static count to compare.
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).parent
SKIP_DIRS = {"node_modules", ".next"}

TAG = re.compile(r"<(/?)(table|thead|tbody|tr|td|th)\b[^>]*>", re.S)


class Table:
    def __init__(self) -> None:
        self.headers = 0
        self.header_generated = False
        self.rows: list[tuple[int, int | None, bool]] = []   # cells, colSpan, generated


def scan(text: str) -> list[Table]:
    """Every table in the file, with its header width and each row's width."""
    stack: list[Table] = []
    done: list[Table] = []
    in_head = False
    row: list[int] | None = None
    row_span: int | None = None
    row_generated = False
    row_start = 0

    for m in TAG.finditer(text):
        closing, tag = m.group(1) == "/", m.group(2)

        if tag == "table":
            if closing:
                if stack:
                    done.append(stack.pop())
            else:
                stack.append(Table())
            continue
        if not stack:
            continue
        table = stack[-1]

        if tag == "thead":
            in_head = not closing
        elif tag == "tr":
            if closing:
                if row is not None and not in_head:
                    table.rows.append((len(row), row_span, row_generated))
                row, row_span, row_generated = None, None, False
            else:
                row, row_span, row_generated = [], None, False
                row_start = m.end()
        elif tag in ("td", "th") and not closing:
            span = re.search(r"colSpan=\{(\d+)\}", m.group(0))
            if in_head:
                table.headers += 1
                table.header_generated = table.header_generated or ".map(" in text[row_start:m.start()]
            elif row is not None:
                row.append(1)
                if span:
                    row_span = int(span.group(1))
                row_generated = row_generated or ".map(" in text[row_start:m.start()]

    done.extend(reversed(stack))
    return done


def check(path: pathlib.Path) -> list[str]:
    problems: list[str] = []
    for table in scan(path.read_text(encoding="utf-8")):
        if not table.headers or table.header_generated:
            continue
        for cells, span, generated in table.rows:
            if generated:
                continue
            if span is not None:
                if span != table.headers:
                    problems.append(
                        f"a spanning row covers {span} of {table.headers} columns"
                    )
                continue
            if cells and cells != table.headers:
                problems.append(f"a row has {cells} cells against {table.headers} headers")
    return problems


def main() -> int:
    bad = checked = 0
    for path in sorted(ROOT.rglob("*.tsx")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if "<thead>" not in path.read_text(encoding="utf-8"):
            continue
        checked += 1
        for problem in check(path):
            print(f"  {path.relative_to(ROOT)}: {problem}")
            bad += 1

    print(f"{checked} file(s) with tables checked, {bad} problem(s).")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
