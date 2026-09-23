from jobhunter.l2.v2.quality import assess


def test_offline_records_are_never_eligible() -> None:
    q = assess(source="usable", evidence="pass")
    assert q["search_eligible"] is False  # semantics/completeness not_checked


def test_full_gate_eligible() -> None:
    q = assess(source="usable", evidence="pass", semantics="no_findings",
               completeness="no_findings", sampling="complete")
    assert q["search_eligible"] is True


def test_human_rejection_is_final() -> None:
    q = assess(source="usable", evidence="pass", semantics="no_findings",
               completeness="no_findings", human_review="rejected")
    assert q["search_eligible"] is False


def test_insufficient_source_is_never_eligible() -> None:
    for usability in ("partial", "placeholder", "empty", "unsupported"):
        q = assess(source=usability, evidence="pass", semantics="no_findings",
                   completeness="no_findings")
        assert q["search_eligible"] is False, usability


def _audited(**over: object) -> dict[str, object]:
    kw: dict[str, object] = {
        "source": "usable", "evidence": "pass",
        "semantics": "no_findings", "completeness": "no_findings",
    }
    kw.update(over)
    return assess(**kw)  # type: ignore[arg-type]


def test_adjudicated_sampling_is_eligible() -> None:
    """validator/16: a complete-but-disagreeing cohort whose audited medoid is
    fully source-supported settles on the audit, not on sampling variance
    (plan Policy). The dimension records that it was adjudicated, not that the
    samples agreed."""
    assert _audited(sampling="adjudicated")["search_eligible"] is True


def test_a_disagreement_is_never_eligible() -> None:
    assert _audited(sampling="disagreement")["search_eligible"] is False


def test_an_incomplete_cohort_is_monitoring_not_a_verdict() -> None:
    """Parsing contract v3 §3/§5: a cohort short of samples settles on the
    candidate it has, and eligibility is the audit's to grant. An unsampled
    document with the same single verified candidate is eligible; a document
    the sampler simply could not finish must not rank below it."""
    assert _audited(sampling="incomplete")["search_eligible"] is True
    assert _audited(sampling="incomplete", blocking_findings=1)["search_eligible"] is False


def test_an_audit_that_did_not_complete_is_never_a_pass() -> None:
    """`audit_error` and an absent audit (`not_checked`) both gate eligibility
    on either dimension (spec §4: invalid JSON, timeout or refusal yields
    audit_error, not a pass)."""
    for dim in ("semantics", "completeness"):
        for value in ("error", "not_checked", "findings"):
            q = _audited(**{dim: value}, sampling="complete")
            assert q["search_eligible"] is False, (dim, value)


def test_blocking_findings_gate_eligibility_without_losing_the_record() -> None:
    q = _audited(sampling="complete", blocking_findings=1)
    assert q["search_eligible"] is False
    assert q["sampling"] == "complete" and q["semantics"] == "no_findings"  # still visible
    assert _audited(sampling="adjudicated", blocking_unresolved=1)["search_eligible"] is False


def test_only_a_validated_lifecycle_publishes_eligibility() -> None:
    """Spec §6: a published needs-review/quarantined/rejected state is not a
    candidate for the aggregates, whatever its dimensions say. A clean audit of
    a record a human parked (`extract review flag`) or the refuter demoted is
    still a clean audit — it is the settlement that is not `validated` — so the
    lifecycle gates publication on its own."""
    for lifecycle in ("needs_review", "quarantined", "rejected", "pending"):
        q = _audited(sampling="complete", lifecycle=lifecycle)
        assert q["search_eligible"] is False, lifecycle
        assert q["semantics"] == "no_findings"  # the audit stays visible
    assert _audited(sampling="complete", lifecycle="validated")["search_eligible"] is True


def test_lifecycle_is_a_gate_not_a_published_dimension() -> None:
    """The blob's keys are the spec's seven dimensions and nothing else: the
    record schema forbids extra properties."""
    assert set(_audited(lifecycle="needs_review")) == {
        "source", "evidence", "semantics", "completeness", "sampling",
        "human_review", "search_eligible",
    }


def test_an_unrecognized_human_disposition_is_never_a_pass() -> None:
    """`human_review` is checked against the values that mean "no human has
    withheld this record", not against the single value that means rejection:
    a disposition this policy does not know cannot pass by default."""
    assert _audited(human_review="accepted")["search_eligible"] is True
    for disposition in ("rejected", "flagged", "reopened", "pending", ""):
        assert _audited(human_review=disposition)["search_eligible"] is False, disposition
