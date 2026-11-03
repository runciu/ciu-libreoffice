"""Builds examples/ciu-test-orders.ods: a small, deliberately messy table for
trying CIU, with a second sheet of prompts and their expected answers.

Expected answers are computed by reading the saved file back through the
extension's own reader and running reference SQL in its sandbox, so they
match what a correct answer looks like inside CIU.

    /usr/bin/python3 examples/make_test_workbook.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tests", "integration"))
sys.path.insert(0, os.path.join(ROOT, "python", "pythonpath"))

import uno
from lo_harness import Office, prop
from ciu_lo.calc import reader
from ciu_lo.core import sql as S
from ciu_lo.core.context import Table

OUT = os.path.join(HERE, "ciu-test-orders.ods")

HEADER = ("Order ID", "Customer", "Region", "Product", "Units", "Unit price", "Discount")
# Problems on purpose: 1007 duplicated exactly, 1015 duplicated with different
# spacing and case, missing Units (1004), Region "N/A" (1010), price "-" (1018),
# Discount stored as text percentages.
ORDERS = [
    (1001, "Acme Ltd", "North", "Tea", 12, 2.50, "0%"),
    (1002, "Brightway", "South", "Coffee", 5, 7.00, "10%"),
    (1003, "Cobalt & Co", "East", "Mugs", 20, 4.25, "5%"),
    (1004, "Dunmore", "West", "Biscuits", None, 1.80, "0%"),
    (1005, "Acme Ltd", "North", "Coffee", 8, 7.00, "10%"),
    (1006, "Elm Street Cafe", "South", "Tea", 30, 2.40, "15%"),
    (1007, "Fairfield", "East", "Biscuits", 15, 1.80, "0%"),
    (1007, "Fairfield", "East", "Biscuits", 15, 1.80, "0%"),
    (1008, "Greystone", "West", "Mugs", 6, 4.50, "0%"),
    (1009, "Brightway", "South", "Tea", 10, 2.50, "5%"),
    (1010, "Harbour Foods", "N/A", "Coffee", 4, 7.20, "0%"),
    (1011, "Cobalt & Co", "East", "Tea", 25, 2.30, "10%"),
    (1012, "Ivy Lane", "North", "Biscuits", 40, 1.75, "20%"),
    (1013, "Dunmore", "West", "Coffee", 9, 6.90, "5%"),
    (1014, "Elm Street Cafe", "South", "Mugs", 12, 4.25, "10%"),
    (1015, "Juniper Hall", "North", "Tea", 18, 2.45, "5%"),
    (1015, " juniper hall ", "North", "Tea", 18, 2.45, "5%"),
    (1016, "Greystone", "West", "Biscuits", 22, 1.80, "0%"),
    (1017, "Acme Ltd", "North", "Mugs", 3, 4.50, "0%"),
    (1018, "Kestrel Group", "East", "Coffee", 11, "-", "10%"),
    (1019, "Brightway", "South", "Biscuits", 16, 1.85, "0%"),
    (1020, "Harbour Foods", "West", "Tea", 14, 2.50, "5%"),
]

# Reference SQL over the table CIU builds (columns a..h, plus row).
CHECKS = [
    ("Ask", "Summarise", None,
     "Should mention 22 orders across 4 regions and 4 products, a Total row, and the missing or odd values."),
    ("Ask", "Find issues", None,
     "Should list: order 1007 twice; order 1015 twice (spacing and case differ); Units missing for 1004 (row 5); "
     "Region 'N/A' for 1010; Unit price '-' for 1018; Discount stored as text."),
    ("Ask", "Which region had the highest revenue?",
     "SELECT c, SUM(h) AS revenue FROM t WHERE c <> 'N/A' GROUP BY c ORDER BY revenue DESC LIMIT 1", None),
    ("Ask", "How many orders are in the North region?",
     "SELECT COUNT(*) FROM t WHERE LOWER(c) = 'north'",
     "Counts the duplicated 1015 twice; ask 'how many distinct orders' for the other answer."),
    ("Ask", "What is the average unit price of coffee?",
     "SELECT ROUND(AVG(f), 2) FROM t WHERE LOWER(d) = 'coffee'",
     "The '-' price is a missing value, so 4 coffee orders count."),
    ("Ask", "Which customer bought the most units in total?",
     "SELECT b, SUM(e) AS units FROM t GROUP BY LOWER(TRIM(b)) ORDER BY units DESC LIMIT 1", None),
    ("Propose change", "Add a column with revenue after discount", None,
     "Expect a formula like =H2*(1-G2) filled down column I, with a Revenue after discount "
     "header. Calc converts the text '10%' in plain arithmetic, so most rows work; order 1018 "
     "has no revenue, so its cell may show an error, which the result lists. (SUM over column G "
     "would give 0, because range functions skip text.)"),
    ("Propose change", "Remove duplicate orders (same Order ID) into a new sheet",
     "SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY a ORDER BY row) AS n FROM t) WHERE n = 1",
     "Rows kept (the Total row is kept unless you also ask to drop it)."),
    ("Propose change", "Drop rows with missing values into a new sheet",
     "SELECT * FROM t WHERE e IS NOT NULL AND f IS NOT NULL AND c <> 'N/A' AND a IS NOT NULL",
     "Rows kept (removes 1004, 1010, 1018 and the Total row, whose Order ID is empty)."),
    ("Propose change", "Make a new sheet with total revenue per region",
     "SELECT c, SUM(h) AS revenue FROM t WHERE a IS NOT NULL GROUP BY c ORDER BY revenue DESC",
     "One row per region, including N/A."),
]


def build(office):
    doc = office.new_calc()
    sheets = doc.getSheets()
    data = sheets.getByIndex(0)
    data.setName("Orders")
    width = len(HEADER) + 1
    rows = [HEADER + ("Revenue",)]
    for r in ORDERS:
        rows.append(tuple("" if v is None else (float(v) if isinstance(v, (int, float)) else v)
                          for v in r) + ("",))
    data.getCellRangeByPosition(0, 0, width - 1, len(rows) - 1).setDataArray(rows)
    last = len(rows)
    for r in range(2, last + 1):
        data.getCellRangeByName("H%d" % r).setFormula("=IFERROR(E%d*F%d;\"\")" % (r, r))
    total = last + 1
    data.getCellRangeByName("B%d" % total).setString("Total")
    data.getCellRangeByName("E%d" % total).setFormula("=SUM(E2:E%d)" % last)
    data.getCellRangeByName("H%d" % total).setFormula("=SUM(H2:H%d)" % last)
    header = data.getCellRangeByPosition(0, 0, width - 1, 0)
    header.CharWeight = 150.0
    data.getCellRangeByName("F2:F%d" % last).NumberFormat = 2   # 0.00
    data.getCellRangeByName("H2:H%d" % total).NumberFormat = 2
    data.getColumns().OptimalWidth = True
    return doc


def answers(doc):
    data = doc.getSheets().getByName("Orders")
    ctrl = doc.getCurrentController()
    ctrl.select(data.getCellRangeByName("A1"))
    snap = reader.read_full(doc, reader.read_selection(doc, ctrl))
    table = Table(snap)
    out = []
    for kind, prompt, sql, note in CHECKS:
        expected = note or ""
        if sql:
            cols, rows = S.execute(sql, table, 100, 5.0, include_totals=prompt.startswith(
                ("Remove", "Drop", "Make")))
            if prompt.startswith(("Remove", "Drop")):
                expected = "%d rows kept. %s" % (len(rows), note or "")
            elif len(rows) == 1 and len(cols) <= 2:
                expected = " = ".join(S.format_cell(v) for v in rows[0]) + (
                    ". " + note if note else "")
            else:
                expected = "; ".join(", ".join(S.format_cell(v) for v in r) for r in rows) + (
                    ". " + note if note else "")
        out.append((kind, prompt, expected.strip(), sql or ""))
    return out


def write_guide(doc, checks):
    sheets = doc.getSheets()
    sheets.insertNewByName("How to test", 1)
    guide = sheets.getByName("How to test")
    rows = [("CIU test workbook", "", "", ""),
            ("Click any cell in the Orders table, open CIU > Ask CIU about selection, "
             "type the prompt and press the button shown.", "", "", ""),
            ("", "", "", ""),
            ("Button", "Prompt", "Expected", "Reference SQL")]
    rows += checks
    guide.getCellRangeByPosition(0, 0, 3, len(rows) - 1).setDataArray(rows)
    guide.getCellRangeByName("A1").CharWeight = 150.0
    guide.getCellRangeByName("A1").CharHeight = 14.0
    guide.getCellRangeByName("A4:D4").CharWeight = 150.0
    cols = guide.getColumns()
    for i, width in enumerate((3500, 9000, 16000, 16000)):
        cols.getByIndex(i).Width = width
    guide.getCellRangeByName("A5:D%d" % len(rows)).IsTextWrapped = True


def main():
    office = Office(port=2170).start()
    try:
        doc = build(office)
        checks = answers(doc)
        write_guide(doc, checks)
        doc.getCurrentController().setActiveSheet(doc.getSheets().getByName("Orders"))
        if os.path.exists(OUT):
            os.remove(OUT)
        doc.storeToURL(uno.systemPathToFileUrl(OUT), (prop("FilterName", "calc8"),))
        doc.close(True)
        for c in checks:
            print("%-15s %-58s %s" % c[:3])
        print(OUT)
    finally:
        office.stop()


if __name__ == "__main__":
    main()
