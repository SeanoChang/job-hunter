from jobhunter.sources.ashby import Ashby
from jobhunter.sources.base import Source
from jobhunter.sources.greenhouse import Greenhouse
from jobhunter.sources.lever import Lever

SOURCES: dict[str, Source] = {
    "greenhouse": Greenhouse(),
    "lever": Lever(),
    "ashby": Ashby(),
}


def get_source(name: str) -> Source:
    return SOURCES[name]
