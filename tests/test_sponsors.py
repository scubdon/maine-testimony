import zipfile
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from maine_testimony.cli import app
from maine_testimony.db import Store
from maine_testimony.sponsors import (
    load_people,
    primary_sponsors,
    resolve,
    session_of,
    session_window,
)

pytest.importorskip("yaml")

# Fictional legislators. 128th = Dec 2016 - Nov 2018; party is read on Feb 1, 2017.
PEOPLE = {
    "legislature/Ada-Quill.yml": """
id: ocd-person/aaa
name: Ada Quill
given_name: Ada
family_name: Quill
party:
- name: Democratic
roles:
- {start_date: 2016-12-07, type: lower, district: '1'}
other_names:
- name: Representative Quill of Fairport
""",
    "retired/Bo-Quill.yml": """
id: ocd-person/bbb
name: Bo Quill
given_name: Bo
family_name: Quill
party:
- name: Republican
roles:
- {start_date: 2016-12-07, end_date: 2018-12-05, type: lower, district: '2'}
""",
    "retired/Cy-Quill.yml": """
id: ocd-person/ccc
name: Cy Quill
family_name: Quill
party:
- name: Republican
roles:
- {end_date: 2018-12-05, type: upper, district: '3'}
""",
    "retired/Dee-Marsh.yml": """
id: ocd-person/ddd
name: Dee Marsh
family_name: Marsh
party:
- {name: Republican, end_date: '2019-01-03'}
- name: Independent
roles:
- {start_date: 2014-12-03, end_date: 2020-12-02, type: lower, district: '4'}
""",
    "retired/Eli-Fenn.yml": """
id: ocd-person/eee
name: Eli Fenn
family_name: Fenn
party:
- name: Democratic
roles:
- {start_date: 2016-12-07, end_date: 2018-12-05, type: lower, district: '5'}
""",
    "retired/Eli-R-Fenn.yml": """
id: ocd-person/eee2
name: Eli R. Fenn
family_name: Fenn
party:
- name: Democratic
roles:
- {end_date: 2018-12-05, type: lower, district: '5'}
""",
    "retired/Old-Timer.yml": """
id: ocd-person/old
name: Gus Fairport
family_name: Fairport
party:
- name: Republican
roles:
- {start_date: 2008-12-03, end_date: 2010-12-01, type: lower, district: '6'}
""",
}

BILLS = (
    (
        "id,identifier,title,classification,subject,session_identifier,jurisdiction,"
        "organization_classification\n"
    )
    + """b1,LD 11,An Act One,['bill'],[],{s},Maine,lower
b2,LD 12,An Act Two,['bill'],[],{s},Maine,lower
b3,LD 13,An Act Three,['bill'],[],{s},Maine,upper
b4,LD 14,An Act Four,['bill'],[],{s},Maine,lower
b5,LD 15,An Act Five,['bill'],[],{s},Maine,lower
b6,LD 16,An Act Six,['bill'],[],{s},Maine,lower
b7,LD 17,An Act Seven,['bill'],[],{s},Maine,lower
b8,HP 900,Joint Order,['bill'],[],{s},Maine,lower
"""
)
SPONSORS = """id,name,entity_type,organization_id,person_id,bill_id,primary,classification
1,{n1},person,,{pid},b1,True,primary
2,Ada QUILL,person,,,b2,True,primary
3,QUILL,person,,,b3,True,primary
4,QUILL,person,,,b4,True,primary
5,MARSH,person,,,b5,True,primary
6,FENN,person,,,b6,True,primary
7,FAIRPORT,person,,,b7,True,primary
8,MARSH,person,,,b1,False,cosponsor
9,Ada QUILL,person,,,b8,True,primary
"""
VERSIONS = "id,bill_id,note,date,classification,extras\n" + "".join(
    f"v{i},b{i},Text,,,{{}}\n" for i in range(1, 9)
)
LINKS = "id,media_type,url,version_id\n" + "".join(
    f"l{i},application/pdf,http://example.test/getPDF.asp?paper={p}&item=1&snum=128,v{i}\n"
    for i, p in enumerate(
        ["HP0011", "HP12", "SP0013", "HP0014", "HP0015", "HP0016", "HP0017", "HP0900"], 1
    )
)


@pytest.fixture
def people_dir(tmp_path: Path) -> Path:
    root = tmp_path / "people" / "data" / "me"
    for rel, body in PEOPLE.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body)
    return root


def _export(tmp_path: Path, session: int, *, n1="Eli FENN", pid="", zipped=False) -> Path:
    files = {
        "bills": BILLS.format(s=session),
        "bill_sponsorships": SPONSORS.format(n1=n1, pid=pid),
        "bill_versions": VERSIONS,
        "bill_version_links": LINKS,
    }
    name = f"ME_{session}_csv_testhash"
    if zipped:
        path = tmp_path / f"{name}.zip"
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as z:
            for k, v in files.items():
                z.writestr(f"ME/{session}/ME_{session}_{k}.csv", v)
        return path
    d = tmp_path / name / "ME" / str(session)
    d.mkdir(parents=True)
    for k, v in files.items():
        (d / f"ME_{session}_{k}.csv").write_text(v)
    return tmp_path / name


def test_session_window_and_name():
    assert session_window(128) == (date(2016, 12, 1), date(2018, 11, 30))
    assert session_window(132)[0] == date(2024, 12, 1)
    assert session_of(Path("data/external/ME_131_csv_2INJcOpag7rI9IvTG4gZT.zip")) == 131
    with pytest.raises(ValueError):
        session_of(Path("people.zip"))


def test_load_people(people_dir):
    people = load_people(people_dir)
    ada = people["ocd-person/aaa"]
    # "Representative Quill of Fairport" must not make "fairport" one of her surnames.
    assert ada.surnames == {"quill"} and "fairport" not in ada.surnames
    assert ada.firsts >= {"ada"}
    marsh = people["ocd-person/ddd"]
    assert marsh.party_on(date(2017, 2, 1)) == "Republican"
    assert marsh.party_on(date(2019, 2, 1)) == "Independent"


def test_primary_sponsors_reads_ld_bills_and_papers(tmp_path):
    sp = primary_sponsors(_export(tmp_path, 128)).set_index("ld")
    assert list(sp.index) == [11, 12, 13, 14, 15, 16, 17]  # the joint order (no LD) is dropped
    assert sp.loc[12, "paper_number"] == "HP0012"  # zero-padded like ours
    assert sp.loc[11, "sponsor_name"] == "Eli FENN"  # the primary, not the cosponsor


def test_resolve_match_rules(tmp_path, people_dir):
    people = load_people(people_dir)
    rows = {r["ld"]: r for r in resolve(primary_sponsors(_export(tmp_path, 128)), people, 128)}
    got = {ld: (r["match"], r["person_id"], r["party"]) for ld, r in rows.items()}
    # Chamber drops Senator Cy, then the first name picks Ada over Bo.
    assert got[12] == ("surname_chamber_first", "ocd-person/aaa", "Democratic")
    assert got[13] == ("surname_chamber", "ocd-person/ccc", "Republican")  # SP: the Senator
    assert got[14] == ("ambiguous", None, None)  # House, surname only, D vs R
    assert got[15] == ("surname", "ocd-person/ddd", "Republican")  # before the switch
    assert got[16] == ("same_party", None, "Democratic")  # two records of one person
    # Gus Fairport served 2008-10 only, so a 128th sponsor named Fairport is not him.
    assert got[17] == ("unmatched", None, None)

    rows = resolve(primary_sponsors(_export(tmp_path / "x", 129)), people, 129)
    assert next(r for r in rows if r["ld"] == 15)["party"] == "Independent"  # after it


def test_resolve_uses_open_states_person_id(tmp_path, people_dir):
    people = load_people(people_dir)
    src = _export(tmp_path, 130, n1="Somebody ELSE", pid="ocd-person/bbb", zipped=True)
    row = next(r for r in resolve(primary_sponsors(src), people, 130) if r["ld"] == 11)
    assert (row["match"], row["person_name"], row["party"]) == (
        "person_id",
        "Bo Quill",
        "Republican",
    )


def test_import_sponsors_cli(tmp_path, people_dir):
    db = tmp_path / "t.sqlite"
    folder, zipped = _export(tmp_path, 128), _export(tmp_path / "z", 128, zipped=True)
    args = ["import-sponsors", "--db", str(db), "--people", str(people_dir)]
    r = CliRunner().invoke(app, [*args, str(folder)])
    assert r.exit_code == 0, r.output
    assert "128: 7 bills" in r.output
    first = Store(db).conn.execute("SELECT * FROM bill_sponsors ORDER BY ld").fetchall()
    # Re-importing replaces the session's rows, and the zip reads the same as the folder.
    r = CliRunner().invoke(app, [*args, str(zipped)])
    assert r.exit_code == 0, r.output
    again = Store(db).conn.execute("SELECT * FROM bill_sponsors ORDER BY ld").fetchall()
    assert [tuple(x) for x in again] == [tuple(x) for x in first] and len(again) == 7
