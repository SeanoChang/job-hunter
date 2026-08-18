from jobhunter.sources.base import Source
from jobhunter.sources.greenhouse import Greenhouse
from jobhunter.sources.lever import Lever

SOURCES: dict[str, Source] = {
    "greenhouse": Greenhouse(),
    "lever": Lever(),
}


def get_source(name: str) -> Source:
    return SOURCES[name]
