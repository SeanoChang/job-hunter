from jobhunter.sources.ashby import Ashby
from jobhunter.sources.base import Source, TwoPhaseSource
from jobhunter.sources.greenhouse import Greenhouse
from jobhunter.sources.lever import Lever

SOURCES: dict[str, Source] = {
    "greenhouse": Greenhouse(),
    "lever": Lever(),
    "ashby": Ashby(),
}

# Two-phase adapters (list + detail, spec §3.2). Empty until the first one
# lands: with nothing registered here `fetch.py` never takes the two-phase path.
TWO_PHASE_SOURCES: dict[str, TwoPhaseSource] = {}


def get_source(name: str) -> Source:
    return SOURCES[name]


def get_two_phase(name: str) -> TwoPhaseSource | None:
    """The two-phase adapter for a source name, or None if the source is single-phase."""
    return TWO_PHASE_SOURCES.get(name)
