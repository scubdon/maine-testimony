# ruff: noqa: E501  (verbatim page markup)
"""Trimmed copies of display_ps.asp bill status pages, keeping the real markup quirks
(unclosed <tr>/<td> in the divided-report table). All legislators are fictional."""

from __future__ import annotations

_HEAD = """<html><head><meta charset="iso-8859-1"></head><body>
<a href="//legislature.maine.gov/LawMakerWeb/summary.asp?paper={paper}">Chamber Status</a>
<main id="main"><div id="targetSections" class="story">
<div id="sec0" class="data_block">
<span class="story_heading nonprinting">Documents and Disposition</span><span class="lightblacktext printing">LD {ld}, {chamber} {num}</span>
<span class="tlnk-bill odd_row nonprinting"><span class="inlineHeading">Text</span><br>LD {ld}</span>
"""

_FINAL = """<p><span class="story_heading">Final Disposition</span><span class="tlnk-final even_row">{final}{gov}</span></p>"""
_GOV = """<br><span class="inlineHeading">Governor's Action:</span> {gov}"""
_CHAPTER = """<p><span class="story_heading">Chaptered Law</span><span class="tlnk-bill odd_row" ><span class="tlnk-dnld"><span class="inlineHeading">{law} <br class="nonprinting"><span class="printing">, </span>Chapter {chapter}</span></span><span class="tlnk-pdf nonprinting"><a class="pdf_btn" href="getPDF.asp">Printed Chapter PDF</a></span></span></p>"""

_COMMITTEE = """
<p align="center" class="blacktext nonprinting">Need a paper copy? Contact the Document Room.</p>
</div><div id="sec1" class="data_block"><span class="inlineHeading">Amendments to LD {ld}</span>
</div><div id="sec3" class="data_block"><p class="printing leading" /><span class="story_heading">Status In Committee</span><p><span class="inlineHeading">Referred to</span> <span class="inlineData">Committee on Marine Resources</span> <span class="inlineHeading">on</span> <span class="inlineData">Jan 23, 2025.</span><br /><span class="inlineHeading">Latest Committee Action:</span> <span class="inlineData">{action}</span>
<br /><span class="inlineHeading">Latest Committee Report:</span><span class="inlineData"> {report}</span><p id="phws-list">Loading....</p><TABLE border="0" name="CDtab"><TR><TH>Date</TH></TR><tr><td class="data_centered even_row">Mar 5, 2025</td><td>Work Session Held</td></tr></TABLE>
</div><div id="sec9" class="data_block">{divided}</div><div id="sec4" class="data_block"></div>
</div></main></body></html>"""

_NO_DIVIDED = """<span class="story_heading">Divided Reports</span><p class="inlineHeading">No Divided Reports.</p>"""


def _divided(reports: list[tuple[str, list[str]]]) -> str:
    rows = [
        '<table class="reportsTable"><tr><th colspan="2"><span class="story_heading">'
        "Divided Reports</span></th></tr><tr><th>Report</th><th>Report Signer</th></tr>"
    ]
    for label, signers in reports:
        first, *rest = signers
        rows.append(f'<tr class="newrow"><td valign="top">{label}</td><td>{first}<br />')
        rows += [f'<tr><td valign="top">&nbsp;</td><td>{s}<br />' for s in rest]
        rows.append("</td></tr>")
    return "".join(rows) + "\t</table>"


def bill_page(
    ld: int,
    paper: str,
    *,
    final: str | None,
    gov: str | None = None,
    law: tuple[str, int] | None = None,
    action: str = "Reported Out, Mar 12, 2025, OTP",
    report: str = "Mar 12, 2025; Ought To Pass",
    divided: list[tuple[str, list[str]]] | None = None,
) -> str:
    chamber, num = paper[:2], int(paper[2:])
    page = _HEAD.format(ld=ld, paper=paper, chamber=chamber, num=num)
    if final:
        page += _FINAL.format(final=final, gov=_GOV.format(gov=gov) if gov else "")
    if law:
        page += _CHAPTER.format(law=law[0], chapter=law[1])
    return page + _COMMITTEE.format(
        ld=ld, action=action, report=report, divided=_divided(divided) if divided else _NO_DIVIDED
    )


ENACTED = bill_page(
    101,
    "HP0101",
    final="Emergency Enacted, Mar 21, 2025",
    gov="Emergency Signed, Mar 21, 2025",
    law=("ACTPUB", 8),
)

MAJ = [f"Representative Ada Example{i} of Portland" for i in range(7)]
DIVIDED_DIED = bill_page(
    202,
    "SP0202",
    final="Died Between Houses, Jun 17, 2025",
    action="Reported Out, Jun 12, 2025, OTP-AM/ONTP",
    report="Jun 12, 2025; MAR, MAJ: Ought To Pass As Amended; MAR, MIN: Ought Not To Pass",
    divided=[
        ("MAJ - Ought To Pass As Amended", ["Senator Ben Sample of Knox, Chair", *MAJ]),
        (
            "MIN - Ought Not To Pass",
            [f"Representative Cy Placeholder{i} of Union" for i in range(5)],
        ),
    ],
)

ONTP = bill_page(
    303,
    "HP0303",
    final="Ought Not to Pass Pursuant To Joint Rule 310, Apr 11, 2025",
    action="Reported Out, Mar 28, 2025, ONTP",
    report="Mar 28, 2025; Ought Not To Pass",
)

OPEN = bill_page(404, "HP0404", final=None, action="Work Session Held", report="")

NOT_A_BILL = (
    "<html><body><main id='main'>The page you requested is unavailable.</main></body></html>"
)
