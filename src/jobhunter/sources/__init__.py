from jobhunter.sources.ashby import Ashby
from jobhunter.sources.base import Source, TwoPhaseSource
from jobhunter.sources.greenhouse import Greenhouse
from jobhunter.sources.lever import Lever
from jobhunter.sources.workday import Workday

SOURCES: dict[str, Source] = {
    "greenhouse": Greenhouse(),
    "lever": Lever(),
    "ashby": Ashby(),
}

# Two-phase adapters (list + detail, spec §3.2). `fetch.py` only takes the
# two-phase path for a source registered here.
TWO_PHASE_SOURCES: dict[str, TwoPhaseSource] = {
    "workday": Workday(),
}


def get_source(name: str) -> Source:
    return SOURCES[name]


def get_two_phase(name: str) -> TwoPhaseSource | None:
    """The two-phase adapter for a source name, or None if the source is single-phase."""
    return TWO_PHASE_SOURCES.get(name)
