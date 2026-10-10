import pytest

from maine_testimony.classify import Category, classify, match_town

C = Category


@pytest.mark.parametrize(
    "value, expected, town",
    [
        # people who typed their town (the majority of the dataset)
        ("Portland", C.TOWN, "Portland"),
        ("topsham", C.TOWN, "Topsham"),
        ("WESTBROOK", C.TOWN, "Westbrook"),
        ("Bangor, Maine", C.TOWN, "Bangor"),
        ("Durham ME 04222", C.TOWN, "Durham"),
        ("South Portland", C.TOWN, "South Portland"),  # a city, not "South" + Portland
        ("West Baldwin", C.TOWN, "Baldwin"),  # village -> parent town
        ("Swan's Island", C.TOWN, "Swans Island"),
        ("Saint George", C.TOWN, "St. George"),
        ("Orringon", C.TOWN, "Orrington"),  # typo, fuzzy match
        ("Argyle Twp.", C.TOWN, "Argyle"),
        ("Gorham/York", C.TOWN, "Gorham"),
        # institutions
        ("Maine State Legislature", C.LEGISLATOR, None),
        ("Rep. Jane Doe", C.LEGISLATOR, None),
        ("Department of Health and Human Services", C.GOVERNMENT, None),
        ("DACF", C.GOVERNMENT, None),
        ("Town of Sanford", C.GOVERNMENT, None),
        ("Finance Authority of Maine", C.GOVERNMENT, None),
        (
            "Permanent Comission on the Status of Racial, Indigenous, and Tribal Populations",
            C.GOVERNMENT,
            None,
        ),
        ("Maine Municipal Association", C.ORGANIZATION, None),  # nonprofit, not a town
        ("Friends of Merrymeeting Bay", C.ORGANIZATION, None),
        ("Penobscot Bay YMCA", C.ORGANIZATION, None),
        ("MOFGA", C.ORGANIZATION, None),
        ("MaineHealth", C.ORGANIZATION, None),
        ("Kennebec Cheesery", C.ORGANIZATION, None),
        ("Wilton/MPAC", C.ORGANIZATION, "Wilton"),  # person from Wilton for MPAC
        # placeholders and others
        ("N/A", C.NONE, None),
        ("test", C.NONE, None),
        ("", C.NONE, None),
        ("NULL", C.NONE, None),
        ("Morgantown, West Virginia", C.PLACE_OTHER, None),
        ("Waypoint", C.UNKNOWN, None),
    ],
)
def test_classify(value, expected, town):
    c = classify(value)
    assert c.category == expected, c
    assert c.town == town


def test_own_name_is_none():
    assert classify("Ada Example", "Ada", "Example").category == C.NONE


def test_every_result_names_its_rule():
    assert classify("Portland").rule == "town_exact"
    assert classify("Orringon").rule == "town_fuzzy"
    assert classify("Kennebec Cheesery").rule == "fallback_multiword"


def test_fuzzy_does_not_grab_short_or_multiword_values():
    assert match_town("Wayn") is None  # too short to fuzz (Wayne)
    assert match_town("Maine Audubon") is None
