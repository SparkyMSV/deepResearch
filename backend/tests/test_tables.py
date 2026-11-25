from app import tables
from app.fields import _why_not
from app.pdf import norm

PAGE = """## NOTES TO CONSOLIDATED FINANCIAL STATEMENTS
The Mindspace Group's adjusted net debt to equity ratio is as follows:
<table>
<tr><th>Particulars</th><th>As at</th><th>As at</th></tr>
<tr><td></td><td>March 31, 2026</td><td>March 31, 2025</td></tr>
<tr><td>Gross debt</td><td>1,30,386.42</td><td>1,01,420.80</td></tr>
<tr><td>Less: Cash and cash equivalents</td><td>(12,175.90)</td><td>(6,379.31)</td></tr>
<tr><td>Adjusted net debt</td><td>1,18,210.52</td><td>95,041.50</td></tr>
<tr><td>Adjusted net debt to adjusted equity ratio</td><td>0.75</td><td>0.64</td></tr>
</table>
<table>
<tr><th>Particulars</th><th>As at March 31, 2026</th><th>As at March 31, 2025</th></tr>
<tr><td>Opening balance</td><td>155.17</td><td>128.00</td></tr>
<tr><td>Closing balance</td><td>148.94</td><td>155.17</td></tr>
</table>"""


def test_cell_by_row_label_and_column_heading():
    c = tables.cell(PAGE, "Adjusted net debt", "March 31, 2026")
    assert (c.value, c.row, c.column) == ("1,18,210.52", "Adjusted net debt", "As at March 31, 2026")
    assert tables.cell(PAGE, "adjusted net debt", "2025").value == "95,041.50"
    assert tables.cell(PAGE, "Closing balance", "March 31, 2025").value == "155.17"


def test_quote_from_cell_passes_the_quote_check():
    c = tables.cell(PAGE, "Gross debt", "2026")
    assert _why_not(c.value, c.quote, norm(PAGE)) is None


def test_unclear_row_or_column_explained():
    assert "rows" in tables.cell(PAGE, "Total borrowings", "2026").error  # lists the row labels
    assert "Adjusted net debt to adjusted equity ratio" in tables.cell(PAGE, "net debt", "2026").error  # several rows
    assert "As at March 31, 2025" in tables.cell(PAGE, "Gross debt", "As at").error  # several columns
    assert "no table" in tables.cell("plain text page", "Gross debt", "2026").error


def test_quote_check_ignores_digit_grouping():
    assert _why_not("118210.52", "Adjusted net debt ... 118,210.52", norm(PAGE)) is None
