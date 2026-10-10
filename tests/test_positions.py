import pytest

from maine_testimony.positions import Position, classify_position

P = Position


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Testimony in Support of LD 550, An Act to ...", P.SUPPORT),
        ("Testimony IN OPPOSITION to LD 827 and LD 987.", P.OPPOSE),
        ("Re: Opposition to LD 257, An Act to Eliminate ...", P.OPPOSE),
        ("Dear Committee Members, I oppose LD 754 and LD 1519, which ...", P.OPPOSE),
        ("Neither For Nor Against LD 210, An Act Making ...", P.NEITHER),
        # NFNA is checked first, even though the text later says "oppose"
        (
            "Today I am testifying neither for, nor against LD 356. Originally I was prepared to "
            "oppose the original version.",
            P.NEITHER,
        ),
        ("Testimony of Senator Alex Doe introducing LD 1019, An Act to Fund ...", P.SUPPORT),
        ("Testimony of Representative Pat Roe presenting L.D. 1052, Resolution ...", P.SUPPORT),
        ("I urge you to vote ought to pass on this bill.", P.SUPPORT),
        ("We ask the committee to vote Ought Not to Pass.", P.OPPOSE),
        # earliest explicit statement wins over a later caveat
        ("We oppose LD 12 as drafted. We would support LD 12 if amended.", P.OPPOSE),
        # supporting "the intent" is not support
        ("While we support the intent of the bill, the cost is too high.", P.UNKNOWN),
        ("I am submitting this testimony to voice my concerns about the cuts.", P.UNKNOWN),
    ],
)
def test_positions(text, expected):
    assert classify_position(text).position == expected


def test_evidence_and_offset_recorded():
    r = classify_position("Hello. Testimony in support of LD 5.")
    assert r.position == P.SUPPORT
    assert "support of LD 5" in r.evidence
    assert r.char_offset == 17


def test_sponsor_method_is_flagged():
    assert classify_position("Senator X introducing LD 9").method == "regex_sponsor"


def test_empty_text():
    r = classify_position("   ")
    assert r.position == P.UNKNOWN
    assert r.method == "no_text"
