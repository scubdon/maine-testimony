from maine_testimony.orgs import cluster_orgs, org_key


def test_org_key_normalization():
    assert org_key("The Professional Logging Contractors of the Northeast") == org_key(
        "Professional Logging Contractors of the Northeast"
    )
    assert org_key("Maine Medical Assn.") == "maine medical association"
    assert org_key("Acme Widgets, Inc.") == "acme widgets"
    assert org_key("Department of Marine Resources") == "maine department of marine resources"
    assert org_key("The Nature Conservancy in Maine") == "nature conservancy"


def test_aliases():
    assert org_key("NRCM") == "natural resources council of maine"
    assert org_key("MSEA-SEIU Local 1989") == org_key(
        "Maine Service Employees Association, SEIU Local 1989"
    )


def test_cluster_merges_typos_and_picks_common_spelling():
    names = ["Natural Resources Council of Maine"] * 3 + ["Natural Resource Council of Maine"]
    out = cluster_orgs(names)
    keys = {out[n][0] for n in names}
    assert len(keys) == 1
    assert out["Natural Resource Council of Maine"][1] == "Natural Resources Council of Maine"


def test_ambiguous_acronyms_stay_separate():
    out = cluster_orgs(["MMA", "Maine Municipal Association", "Maine Medical Association"])
    assert len({v[0] for v in out.values()}) == 3
