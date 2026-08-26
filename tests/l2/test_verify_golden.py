import json
from pathlib import Path

from jobhunter.l2 import verify

HERE = Path(__file__).parent / "fixtures"
GOLDEN_MD = Path(__file__).resolve().parents[1] / "fixtures" / "md"


def _case(md_path: Path, extraction_path: Path) -> None:
    md = md_path.read_text(encoding="utf-8")
    extraction = json.loads(extraction_path.read_text(encoding="utf-8"))
    report = verify(extraction, md)
    assert report.status == "pass", [f"{f.check}:{f.code}@{f.path}" for f in report.findings]


def test_anthropic_golden() -> None:
    _case(GOLDEN_MD / "greenhouse_anthropic.md", HERE / "anthropic.extraction.json")


def test_cjk_golden() -> None:
    _case(HERE / "cjk.md", HERE / "cjk.extraction.json")


def test_cjk_spans_are_codepoints() -> None:
    md = (HERE / "cjk.md").read_text(encoding="utf-8")
    extraction = json.loads((HERE / "cjk.extraction.json").read_text(encoding="utf-8"))
    quote = extraction["demand_profile"]["areas"][0]["claims"][1]["quote"]
    s, e = quote["span"]
    assert md[s:e] == quote["text"] == "分散システムの運用経験"
    assert e - s == 11  # codepoints, not bytes (33 in UTF-8)
    assert len(quote["text"].encode("utf-8")) == 33
