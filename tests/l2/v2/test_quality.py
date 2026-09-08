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
