"""LLM fallback for regex-unknown positions. No test calls the API: a fake classifier
stands in for ClaudePositions, and ClaudePositions itself gets a fake SDK client."""

import json
from types import SimpleNamespace

import pandas as pd
import pytest
from typer.testing import CliRunner

from maine_testimony.cli import app
from maine_testimony.db import Store
from maine_testimony.evaluate import evaluate_position_labels
from maine_testimony.llm_positions import (
    MODEL,
    PROMPT_VERSION,
    ClaudePositions,
    LLMAnswer,
    LLMError,
    LLMFallback,
    LLMRefusal,
    apply_answer,
    locate_quote,
)
from maine_testimony.pipeline import export_public, run_positions
from maine_testimony.positions import HEAD_CHARS, Position

IMPLICIT = (
    "Dear Senator Example and members of the committee, my family has fished out of "
    "Stonington for four generations. LD 404 would end that. Please do not let this "
    "become law."
)
VAGUE = "I am writing about LD 505 and the funding it provides for rural libraries."


class FakeClassifier:
    model = MODEL
    prompt_version = PROMPT_VERSION

    def __init__(
        self,
        answers: dict[str, LLMAnswer] | None = None,
        fail: bool = False,
        refuse: bool = False,
    ):
        self.answers = answers or {}
        self.fail = fail
        self.refuse = refuse
        self.calls: list[str] = []

    def classify(self, head: str) -> LLMAnswer:
        self.calls.append(head)
        if self.fail:
            raise LLMError("RateLimitError: still rate limited")
        if self.refuse:
            raise LLMRefusal("bio")
        for needle, answer in self.answers.items():
            if needle in head:
                return answer
        return LLMAnswer(Position.UNKNOWN, "")


OPPOSE_IMPLICIT = {"Stonington": LLMAnswer(Position.OPPOSE, "Please do not let this become law.")}


@pytest.mark.parametrize(
    "quote, found",
    [
        ("Please do not let this become law.", True),
        ("please DO NOT let this   become law", True),  # case and whitespace
        ("“Please do not let this become law.”", True),  # wrapped in curly quotes
        ("LD 404 would end that...", True),  # trailing ellipsis
        ("Please vote ought not to pass.", False),  # not in the text
        ("LD 404 ... become law", False),  # elided span
        ("law.", False),  # too short to mean anything
    ],
)
def test_locate_quote(quote, found):
    assert (locate_quote(IMPLICIT, quote) is not None) is found


def test_locate_quote_maps_curly_quotes_in_text():
    head = "We say “no” to LD 9 and ask you to reject it."
    start, end = locate_quote(head, 'We say "no" to LD 9')
    assert head[start:end] == "We say “no” to LD 9"


def test_apply_answer_keeps_only_verified_quotes():
    ok = apply_answer(IMPLICIT, "oppose", "please do not let this become law")
    assert ok.position is Position.OPPOSE and ok.method == "llm"
    assert ok.evidence == "Please do not let this become law"  # the document's own text
    assert IMPLICIT[ok.char_offset :].startswith(ok.evidence)

    made_up = apply_answer(IMPLICIT, "oppose", "I strongly oppose LD 404.")
    assert made_up.position is Position.UNKNOWN and made_up.method == "llm_unverified"
    assert made_up.evidence is None

    none = apply_answer(IMPLICIT, "unknown", "")
    assert none.position is Position.UNKNOWN and none.method == "llm"


def _docs(store: Store) -> None:
    store.save_document(1, text="Testimony in support of LD 101.")
    store.save_document(2, text=IMPLICIT)
    store.save_document(3, text=IMPLICIT)  # same form letter: one call for both
    store.save_document(4, text=VAGUE)
    store.save_document(5, text="", error="no_text_layer")


def _positions(store: Store) -> dict[int, tuple]:
    return {
        r[0]: tuple(r[1:])
        for r in store.conn.execute(
            "SELECT testimony_id, position, method, evidence FROM positions"
        )
    }


def test_only_regex_unknown_heads_are_sent_and_answers_are_cached(store):
    _docs(store)
    store.save_document(6, text="I write about LD 606. " + "x " * 4000 + "SECRET TAIL")
    fake = FakeClassifier(OPPOSE_IMPLICIT)
    counts = run_positions(store, llm=LLMFallback(store, fake))

    assert counts == {"support": 1, "oppose": 2, "unknown": 3}
    pos = _positions(store)
    assert pos[1][:2] == ("support", "regex")
    assert pos[2] == ("oppose", "llm", "Please do not let this become law.")
    assert pos[3] == pos[2]
    assert pos[4][:2] == ("unknown", "llm")
    assert pos[5][:2] == ("unknown", "no_text")
    # Docs 2/3 share a call; the regex hit and the empty doc are never sent; long texts
    # are cut to the head.
    assert len(fake.calls) == 3
    assert all(len(h) <= HEAD_CHARS for h in fake.calls)
    assert not any("SECRET TAIL" in h or "support of LD 101" in h for h in fake.calls)

    # Positions are rebuilt from scratch, but the cache survives: no new calls.
    again = FakeClassifier(OPPOSE_IMPLICIT)
    fallback = LLMFallback(store, again)
    assert run_positions(store, llm=fallback) == counts
    assert again.calls == [] and fallback.stats["cached"] == 4
    assert _positions(store) == pos


def test_cache_reused_across_documents_with_the_same_text(store):
    store.save_document(2, text=IMPLICIT)
    run_positions(store, llm=LLMFallback(store, FakeClassifier(OPPOSE_IMPLICIT)))
    store.save_document(7, text=IMPLICIT)  # a later copy of the same form letter
    fake = FakeClassifier()
    run_positions(store, llm=LLMFallback(store, fake))
    assert fake.calls == []
    assert _positions(store)[7][:2] == ("oppose", "llm")


def test_prompt_change_invalidates_cache(store):
    store.save_document(2, text=IMPLICIT)
    run_positions(store, llm=LLMFallback(store, FakeClassifier(OPPOSE_IMPLICIT)))
    newer = FakeClassifier(OPPOSE_IMPLICIT)
    newer.prompt_version = "different"
    run_positions(store, llm=LLMFallback(store, newer))
    assert len(newer.calls) == 1


def test_no_key_uses_cache_only(store):
    _docs(store)
    run_positions(store, llm=LLMFallback(store, FakeClassifier(OPPOSE_IMPLICIT)))
    store.conn.execute("DELETE FROM llm_positions WHERE testimony_id = 4")
    store.conn.commit()

    fallback = LLMFallback(store, None)
    run_positions(store, llm=fallback)
    assert fallback.stats == {"cached": 2, "skipped_no_key": 1}
    pos = _positions(store)
    assert pos[2][:2] == ("oppose", "llm")
    assert pos[4][:2] == ("unknown", "regex_no_match")


def test_limit_and_errors_are_not_cached(store):
    _docs(store)
    limited = LLMFallback(store, FakeClassifier(OPPOSE_IMPLICIT), limit=1)
    run_positions(store, llm=limited)
    assert limited.stats["called"] == 1 and limited.stats["skipped_limit"] >= 1

    failing = LLMFallback(store, FakeClassifier(fail=True))
    run_positions(store, llm=failing)
    assert failing.stats["errors"] >= 1
    assert store.count("llm_positions") == limited.stats["answered"]


def test_refusals_are_cached_and_not_re_asked(store):
    store.save_document(2, text=IMPLICIT)
    store.save_document(3, text=IMPLICIT)
    fallback = LLMFallback(store, FakeClassifier(refuse=True))
    run_positions(store, llm=fallback)
    assert fallback.stats == {"called": 1, "refused": 2}
    assert _positions(store)[2] == ("unknown", "llm_refused", None)
    rows = store.conn.execute("SELECT position, refusal_category FROM llm_positions").fetchall()
    assert [tuple(r) for r in rows] == [("refused", "bio")] * 2

    again = FakeClassifier(OPPOSE_IMPLICIT)
    run_positions(store, llm=LLMFallback(store, again))
    assert again.calls == []
    assert _positions(store)[3][:2] == ("unknown", "llm_refused")


def test_old_cache_table_gets_refusal_column(tmp_path):
    import sqlite3

    db = tmp_path / "old.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        """CREATE TABLE llm_positions (testimony_id INTEGER NOT NULL, text_sha256 TEXT NOT NULL,
           model TEXT NOT NULL, prompt_version TEXT NOT NULL, position TEXT NOT NULL,
           evidence TEXT, created_at TEXT NOT NULL,
           PRIMARY KEY (testimony_id, text_sha256, model, prompt_version))"""
    )
    con.execute("INSERT INTO llm_positions VALUES (2, 'h', 'm', 'v', 'oppose', 'q', 'x')")
    con.commit()
    con.close()

    store = Store(db)
    store.save_llm_positions(
        [(3, "h2")],
        model="m",
        prompt_version="v",
        position="refused",
        evidence=None,
        refusal_category="cyber",
    )
    assert store.llm_position(2, "h", "m", "v")["position"] == "oppose"
    rows = store.conn.execute(
        "SELECT testimony_id, refusal_category FROM llm_positions ORDER BY 1"
    ).fetchall()
    assert [tuple(r) for r in rows] == [(2, None), (3, "cyber")]
    Store(db).close()  # reopening doesn't try to add the column again


def test_unexpected_errors_stop_the_run(store):
    class Broken(FakeClassifier):
        def classify(self, head):
            raise PermissionError("bad key")

    store.save_document(4, text=VAGUE)
    with pytest.raises(PermissionError):
        run_positions(store, llm=LLMFallback(store, Broken()))


def test_export_has_no_llm_evidence_or_text(store, tmp_path):
    _docs(store)
    for tid in range(1, 6):
        store.conn.execute(
            "INSERT INTO testimony (id, session_number, committee_code, hearing_date,"
            " first_seen_at, last_seen_at) VALUES (?, 132, 'MAR', '2025-02-06', 'x', 'x')",
            (tid,),
        )
    run_positions(store, llm=LLMFallback(store, FakeClassifier(OPPOSE_IMPLICIT)))
    export_public(store, tmp_path)
    df = pd.read_parquet(tmp_path / "testimony.parquet")
    assert df.loc[df["testimony_id"] == 2, "position_method"].item() == "llm"
    assert not {"evidence", "text"} & set(df.columns)
    for f in tmp_path.glob("*.csv"):
        assert "Stonington" not in f.read_text() and "become law" not in f.read_text()


def test_evaluate_with_llm(store, tmp_path):
    _docs(store)
    labels = tmp_path / "labels.csv"
    labels.write_text(
        "testimony_id,doc_url,label\n1,u,support\n2,u,oppose\n4,u,unknown\n5,u,unknown\n"
    )
    before = evaluate_position_labels(labels, store)
    assert before["missed"] == 1 and before["positioned"] == 2
    after = evaluate_position_labels(
        labels, store, llm=LLMFallback(store, FakeClassifier(OPPOSE_IMPLICIT))
    )
    assert after["accuracy"] == 1.0 and after["missed"] == 0
    assert after["committed_precision"] == 1.0 and after["methods"]["llm"] == 2


def test_cli_positions_llm_without_key(tmp_path):
    db = tmp_path / "t.sqlite"
    s = Store(db)
    s.save_document(4, text=VAGUE)
    s.close()
    r = CliRunner().invoke(app, ["positions", "--db", str(db), "--llm"])
    assert r.exit_code == 0, r.output
    assert "skipped_no_key=1" in r.output


# ------------------------------------------------------------ ClaudePositions
class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def _response(stop_reason="end_turn", answer=None):
    text = json.dumps(answer or {"position": "oppose", "evidence": "Please do not"})
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
    )


def test_claude_positions_request_and_parse():
    messages = FakeMessages(_response())
    clf = ClaudePositions(SimpleNamespace(messages=messages))
    assert clf.classify(IMPLICIT) == LLMAnswer(Position.OPPOSE, "Please do not")

    kw = messages.kwargs
    assert kw["model"] == "claude-haiku-5-5"
    assert kw["messages"] == [{"role": "user", "content": f"<document>\n{IMPLICIT}\n</document>"}]
    schema = kw["output_config"]["format"]["schema"]
    assert schema["properties"]["position"]["enum"] == [
        "support",
        "oppose",
        "neither",
        "unknown",
    ]


@pytest.mark.parametrize(
    "resp",
    [
        _response(stop_reason="refusal"),
        _response(stop_reason="max_tokens"),
        SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="thinking")]),
        _response(answer={"position": "maybe", "evidence": ""}),
    ],
)
def test_claude_positions_bad_answers_raise_llm_error(resp):
    clf = ClaudePositions(SimpleNamespace(messages=FakeMessages(resp)))
    with pytest.raises(LLMError):
        clf.classify(VAGUE)


def test_claude_positions_refusal_carries_category():
    resp = _response(stop_reason="refusal")
    resp.stop_details = SimpleNamespace(type="refusal", category="bio", explanation="")
    clf = ClaudePositions(SimpleNamespace(messages=FakeMessages(resp)))
    with pytest.raises(LLMRefusal) as exc:
        clf.classify(VAGUE)
    assert exc.value.category == "bio"


def test_from_env_without_key_is_none():
    assert ClaudePositions.from_env() is None  # conftest hides any real key
