# CIU for LibreOffice

A LibreOffice Calc extension that uses the model already running in [CIU](https://runciu.com). Select cells, then ask a question or describe a change. CIU answers, or proposes the change and shows you a preview. Nothing is written until you click Apply, and everything runs on your machine.

Version 0.0.1. Calc only for now.

## Features

**Ask.** Get a streamed answer about the selected cells, or use one of the shortcuts: Summarise, Explain formulas, Find issues.

Questions that need every row, such as "which African country grew most" on a 240-row table, are answered with an exact query. The model writes one SQL `SELECT` over the selection, the extension runs it over all rows, and the result goes back to the model before it answers. The window shows the SQL it ran. If the query fails, the model sees the error and gets one chance to correct it. SQL columns are named after their headers (`unit_price` for "Unit price"); a column without a usable header keeps its letter. Before the SQL, the model says how it reads the values it tests ("paid: Yes, Y = paid; No, empty = unpaid"), and the window shows that next to the SQL, so a misread value is easy to spot.

The SQL runs in a locked-down, in-memory SQLite database:
- Only reading is allowed. Writes, `ATTACH`, `PRAGMA` and extension loading are refused, so it cannot touch your files or the network.
- Queries are stopped after 2 seconds, and at most 20 rows come back.
- Your sheet is never modified by a question.

If LibreOffice's Python has no SQLite, a simpler built-in query form is used instead (filters, a calculated value, sort, top N, aggregates). **CIU > Check CIU connection** shows which one is active.

**Propose change.** CIU returns a plan built from three operations:

| Operation | Effect |
|---|---|
| `set_formula` | one formula in one cell |
| `fill_formula` | a formula filled down a column or across a row, like dragging the fill handle |
| `set_values` | plain text or numbers in a range |

**Clean data.** The **Clean data** button makes a cleaned copy in a new sheet. CIU reads the sheet one column at a time, the way a person would. For each column it decides what the column holds (yes/no, a category, names, identifiers, free text), which values are the standard ones, what every other value means (`yse` and `Y` are `Yes`, `settled` is `Yes`, `TBC` is unknown), and what an empty cell means. For rare values it also sees the row they sit in, so `TBC` is read next to a note such as "awaiting quote". Code checks every answer (a value must exist in the column, and a replacement must be one of the column's standard values or a number in a number column), then applies the rules to every row, so the model never retypes your data.

The preview sorts the changes so you know where to look:
- **Check these:** changes of meaning (`settled` to `Yes`, `TBC` to empty) and what empty cells are read as.
- **Spelling and abbreviations:** values that are near-identical to the standard one (`yse`, `Y`, `Lodnon`).
- **Unresolved:** values still outside the standard ones, left as they are and listed, never silently kept.
- **Standard fixes:** what code fixes without judgement: outer spaces, case differences, numbers and dates stored as text.

Nothing happens until you press **Create sheet**. To change a decision, type it, for example `Clean data: settled means part paid`, and press Clean data again. Find issues suggests Clean data when it would fix something.

**Clean into a new sheet.** Ask for things like "remove duplicate countries", "make a copy without the rows that have missing values", "fill missing Units with the average", or "a table of total population per region". CIU writes one SQL `SELECT` (in the same sandbox as Ask), and the result goes to a **new sheet**; the original sheet is never changed. The preview shows how many rows are kept, names the rows left out when it can, says total rows are not copied, and shows the SQL and the first rows. **Create sheet** adds the new sheet next to the original, with dates and percentages formatted as in the source, and one undo removes it. The copy holds values, not formulas.

Before anything is written, the plan is checked:
- Targets must be valid addresses on the current sheet, and operations may not overlap. A plan can have at most 6 operations and 5,000 cells.
- Formulas are normalised to LibreOffice syntax. Formulas with unbalanced quotes or brackets are refused.
- Formulas may only refer to the current document. These are refused: `WEBSERVICE`, `FILTERXML`, `DDE`, `HYPERLINK`, `INFO` and `INDIRECT`, references to other files, and any URL or file path.
- The preview lists every filled cell the change would overwrite.

When you click Apply:
- The whole change is a single undo step.
- Calc checks each new formula. If one is invalid (syntax error, unknown function, circular reference, `#REF!`), the whole change is rolled back.
- Data errors such as `#DIV/0!` are kept and listed.
- If a target cell was edited after the preview, nothing is written.

## How values are read

CIU reads values the way Calc shows them, so a value means the same thing in a formula, in SQL and in a copy:

| In the sheet | CIU reads | Note |
|---|---|---|
| a date (any date format) | `2026-10-02` | dates typed as text are read too when the day and month order is clear |
| `10%` as text, or 0.1 shown as 10% | 0.1 | as in a Calc formula |
| `£1,200.50`, `1,450`, `(120.00)` | 1200.5, 1450, -120 | |
| `00123` | `00123` | a leading zero marks a code, so it stays text |
| TRUE / FALSE | TRUE / FALSE, or 1 / 0 in SQL | so SUM counts TRUE, as in Calc |
| `no`, `No`, ` No ` | `No` in SQL (the most common spelling) | the result says which spellings were merged |
| `-`, `N/A`, `TBC` in a number column | missing | the answer says which values were treated as missing |

Total and subtotal rows are left out of statistics, queries and copies, and wholly empty rows are skipped. If you click one cell and more data continues below a blank row, the selection line says so. Title rows above the header (a report name, an "As of" date) are not treated as data; their text is passed along as context, so "overdue on the As of date" still works. A comparison such as `paid = ''` on a column with empty cells is refused with the fix (`paid IS NULL`), because empty cells are NULL in SQL and would silently be left out.

## Requirements

- LibreOffice (tested with 24.2)
- [CIU](https://runciu.com) running locally with a model loaded

## Install

1. Download `ciu-libreoffice.oxt` from the Releases page, or build it with `python3 scripts/build_oxt.py`.
2. Double-click the file, or go to **Tools > Extension Manager > Add**.
3. Quit LibreOffice completely and reopen it.
4. Open a spreadsheet and use the **CIU** menu. On macOS, the menu is in the menu bar at the top of the screen.

The extension connects to `http://127.0.0.1:8674`. To use another address, set the `CIU_BASE_URL` environment variable.

## Project layout

```
python/ciu_component.py     UNO entry point
python/pythonpath/ciu_lo/
  core/     no LibreOffice dependency
    ciu_client.py   CIU HTTP client
    context.py      selection to prompt text, with column statistics
    prompts.py      prompts and context budget
    plan.py         plan parsing and validation
    sql.py          sandboxed SQL over all rows
    query.py        built-in query form, used when SQLite is missing
    engine.py       picks between the two
  calc/     reading selections and applying plans
  ui/       the assistant window and main-thread dispatch
registry/Addons.xcu         the CIU menu
```

## How it works

- **Context budget.** The prompt is sized to the context length CIU reports. Column statistics come first, and raw rows fill the space that is left.
- **Arithmetic in code.** Counts, missing values, duplicates, min, max, sum and mean are computed exactly, not by the model. Total rows (Total, World, or a row equal to the sum of the others) are left out.
- **Queries.** The query and the answer share the same prompt prefix, so llama-server reuses its cache instead of reading the selection twice.
- **Structured output.** Plans are requested with a JSON schema, so the model's output is always well-formed. If the server rejects the schema, the client retries without it.
- **Threading.** Requests run on a worker thread. All LibreOffice calls happen on the main thread.
- **No dependencies.** Standard library only, because LibreOffice's bundled Python has no package manager.

## Development

Run all tests:

```
sh scripts/run_tests.sh
```

- The unit tests need only Python 3.
- The integration tests start a headless LibreOffice with a temporary profile, and need a Python that can `import uno`. On macOS that is LibreOffice's bundled Python; on Linux it is the system `python3` with `python3-uno`.
- `test_extension.py` installs a test build (`python3 scripts/build_oxt.py --test`) that includes a small hook for driving the window. Reinstall the normal build afterwards.

To try the extension without a model, run a mock CIU on port 8674 (stop CIU first):

```
python3 tests/mock_ciu.py
```

## Roadmap

- A benchmark suite of common spreadsheet tasks
- A sidebar panel
- Sort, filter, conditional formatting and charts
- Writer and Impress support
