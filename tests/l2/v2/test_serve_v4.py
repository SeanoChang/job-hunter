"""`serve` over a schema-4 record (parsing contract v4 §5): typed skills,
education, the derived authorization and tracks in the digest, and a
`profile_mentions` projection that indexes skills only. Built from the §7
regression fixtures through `serve.profile_of`, the way the store gets them."""

from __future__ import annotations

from typing import Any

from jobhunter.l2.v2 import serve
from tests.l2.v2.conftest import VISA_POLICY
from tests.l2.v2.v4_serving import v4_record


def _digest(name: str) -> dict[str, Any]:
    return serve.summary(serve.profile_of(v4_record(name)))


def test_schema_4_is_a_shape_the_readers_recognise() -> None:
    blob = serve.profile_of(v4_record("visa"))
    assert blob["schema"] == "4"
    assert serve.reads_as_v2(blob)


def test_profile_of_carries_the_derived_authorization_and_the_tracks() -> None:
    record = v4_record("figma")
    blob = serve.profile_of(record)
    assert blob["authorization"] == record["authorization"]
    assert blob["relations"]["tracks"] == record["relations"]["tracks"]
    assert serve.profile_of(blob) == blob  # still idempotent over its own output


def test_a_schema_3_blob_gains_no_authorization_key() -> None:
    from tests.l2.v2.conftest import make_serving_record

    assert "authorization" not in serve.profile_of(make_serving_record())


def test_v1_skills_come_from_the_responsibility_line_and_sponsorship_is_no() -> None:
    out = _digest("visa")
    assert out["mentions"] == ["Kafka", "Docker", "Kubernetes"]
    assert out["mentions_omitted"] == 0
    assert out["authorization"] == {
        "sponsorship": "no",
        "citizenship_required": False,
        "quotes": {"sponsorship": VISA_POLICY, "citizenship": None,
                   "work_authorization": None},
    }
    assert out["tracks"] is None


def test_v2_places_are_not_skills_and_fields_of_study_are_education() -> None:
    out = _digest("lyft")
    assert out["mentions"] == []
    assert out["education"] == {"field_of_study": ["Computer Science"], "credential": []}


def test_the_statement_kind_filter_does_not_apply_to_schema_4() -> None:
    """Typing replaces the schema-3 filter: a `skill` mention on a constraint
    statement is still a skill (the verifier warns on it; the digest lists it)."""
    from tests.l2.v2.conftest import LYFT_MD, assemble4, make_lyft_emit

    blob = serve.profile_of(assemble4(make_lyft_emit(toronto_type="skill"), LYFT_MD))
    assert serve.summary(blob)["mentions"] == ["Toronto"]


def test_v3_tracks_are_named_with_the_open_one_marked() -> None:
    assert _digest("figma")["tracks"] == {
        "selection": "candidate_choice",
        "items": [{"name": "Product", "open": False},
                  {"name": "Backend/Infrastructure", "open": False},
                  {"name": "Security Engineering", "open": False},
                  {"name": "Open", "open": True}],
    }


def test_v4_citizenship_is_its_own_field() -> None:
    auth = _digest("anduril")["authorization"]
    assert auth["sponsorship"] == "undeclared" and auth["citizenship_required"] is True
    assert auth["quotes"]["citizenship"] == (
        "Must be a U.S. Person due to required access to U.S. export-controlled "
        "information or facilities")


def test_v5_a_work_authorization_quote_alone_leaves_sponsorship_undeclared() -> None:
    auth = _digest("work_auth")["authorization"]
    assert auth["sponsorship"] == "undeclared" and auth["citizenship_required"] is False
    assert auth["quotes"] == {"sponsorship": None, "citizenship": None,
                              "work_authorization": "Must be authorized to work in the US."}


def test_v6_may_be_available_reads_yes() -> None:
    assert _digest("may_sponsor")["authorization"]["sponsorship"] == "yes"


def test_schema_4_skills_are_bounded_and_count_what_the_bound_cut() -> None:
    blob = serve.profile_of(v4_record("visa"))
    template = blob["mentions"][0]
    blob["mentions"] = [{**template, "surface": f"skill{i}"}
                        for i in range(serve.MAX_MENTIONS + 2)]
    out = serve.summary(blob)
    assert out["mentions"] == [f"skill{i}" for i in range(serve.MAX_MENTIONS)]
    assert out["mentions_omitted"] == 2


def test_the_schema_4_digest_keeps_the_schema_3_areas_and_facts() -> None:
    blob = serve.profile_of(v4_record("visa"))
    out = serve.summary(blob)
    s3 = serve.summary({**blob, "schema": "3"})
    assert out["areas"] == s3["areas"] and out["facts"] == s3["facts"]
    assert all("importance" not in area for area in out["areas"])


def test_a_schema_4_digest_reads_defensively() -> None:
    out = serve.summary({"schema": "4"})
    assert out["mentions"] == [] and out["mentions_omitted"] == 0
    assert out["education"] == {"field_of_study": [], "credential": []}
    assert out["authorization"] is None and out["tracks"] is None


def test_profile_mentions_index_only_the_skill_typed_mentions() -> None:
    """The aggregate behind `q claims` is a skill index: a field of study or a
    place is not something a posting demands as a skill."""
    assert serve.mention_rows(v4_record("lyft")) == []
    visa = v4_record("visa")
    assert serve.mention_rows(visa) == [
        (surface, "responsibility", serve.NO_IMPORTANCE)
        for surface in ("Kafka", "Docker", "Kubernetes")
    ]
    assert serve.mention_rows(serve.profile_of(visa)) == serve.mention_rows(visa)
