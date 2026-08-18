from jobhunter.sources.base import Source
from jobhunter.sources.greenhouse import Greenhouse

SOURCES: dict[str, Source] = {
    "greenhouse": Greenhouse(),
}


def get_source(name: str) -> Source:
    return SOURCES[name]
