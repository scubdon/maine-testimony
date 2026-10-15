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
        ("Union", C.TOWN, "Union"),  # a Knox County town, not the org keyword
        ("Bath and Phippsburg, Maine", C.TOWN, "Bath"),
        ("North Berwick and Sebago, Maine", C.TOWN, "North Berwick"),
        ("Topsham, ME and Waterford, ME", C.TOWN, "Topsham"),
        ("North Hanson", C.TOWN, "North Anson"),  # typo after a directional
        ("Springvale", C.TOWN, "Springvale"),  # 2000-census CDP (village of Sanford)
        ("Mt Vernon", C.TOWN, "Mount Vernon"),
        # institutions
        ("Maine State Legislature", C.LEGISLATOR, None),
        ("Rep. Jane Doe", C.LEGISLATOR, None),
        ("HD86", C.LEGISLATOR, None),
        ("HD 98", C.LEGISLATOR, None),
        ("H.D. 48", C.LEGISLATOR, None),
        ("SD 12", C.LEGISLATOR, None),
        ("HD 30, Cape Elizabeth", C.LEGISLATOR, None),
        ("District 136", C.LEGISLATOR, None),
        ("District 11 - Waldo County", C.LEGISLATOR, None),
        ("District 4 IAM Union", C.ORGANIZATION, None),  # a union local, not a district
        ("Sioux Falls, SD 57104", C.PLACE_OTHER, None),  # SD + ZIP is not a Senate district
        ("Department of Health and Human Services", C.GOVERNMENT, None),
        ("DACF", C.GOVERNMENT, None),
        ("Town of Sanford", C.GOVERNMENT, None),
        ("Finance Authority of Maine", C.GOVERNMENT, None),
        ("FAME", C.GOVERNMENT, None),
        ("Governor's Energy Office", C.GOVERNMENT, None),
        ("MaineCare Services", C.GOVERNMENT, None),
        ("Southern Aroostook SWCD", C.GOVERNMENT, None),
        ("Oxford County Soil & Water Conservation District", C.GOVERNMENT, None),
        ("Maine School Administrative District 6", C.GOVERNMENT, None),
        ("Regional School Unit 16", C.GOVERNMENT, None),
        ("Cony Middle and High School", C.GOVERNMENT, None),
        ("Portland Public Schools", C.GOVERNMENT, None),
        ("Kittery School Dist.", C.GOVERNMENT, None),
        ("Maine Community College System", C.GOVERNMENT, None),
        ("Ethics Commission", C.GOVERNMENT, None),
        ("Aroostook County Commissioners", C.GOVERNMENT, None),
        (
            "Permanent Comission on the Status of Racial, Indigenous, and Tribal Populations",
            C.GOVERNMENT,
            None,
        ),
        ("Maine Municipal Association", C.ORGANIZATION, None),  # nonprofit, not a town
        # associations of governments / schools are organizations
        ("Maine County Commissioners Association", C.ORGANIZATION, None),
        ("ME Assoc. of Conservation Districts", C.ORGANIZATION, None),
        ("Maine School Board's Association", C.ORGANIZATION, None),
        ("Penobscot Christian School", C.ORGANIZATION, None),  # private
        ("Cheverus High School", C.ORGANIZATION, None),  # private, no "Academy"
        ("Reed & Reed", C.ORGANIZATION, None),  # a company; Reed is also a plantation
        ("Bonny Eagle High School Robotics", C.ORGANIZATION, None),
        ("Support Maine's Public Schools", C.ORGANIZATION, None),  # a campaign
        ("MEDCo", C.ORGANIZATION, None),
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


def test_new_rules_are_named():
    assert classify("North Hanson").rule == "town_fuzzy_directional"
    assert classify("Bath and Phippsburg, Maine").rule == "town_multi"
    assert classify("Cony Middle and High School").rule == "public_school"
    assert classify("Portland Public Schools").rule == "town_schools"
