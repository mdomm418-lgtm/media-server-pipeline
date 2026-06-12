from __future__ import annotations

from .anihls import AnihlsParser
from .bare_episode import BareEpisodeParser
from .base import FilenameParser, ParseResult
from .multi_episode import MultiEpisodeParser
from .release_dot import ReleaseDotParser
from .release_space import ReleaseSpaceParser
from .sonarr_standard import SonarrStandardParser
from .specials import SpecialsParser

_PARSERS: list[FilenameParser] = []


def register(parser: FilenameParser) -> None:
    _PARSERS.append(parser)
    _PARSERS.sort(key=lambda p: p.priority)


def parse_filename(
    filename: str, *, folder_name: str, relative_path: str
) -> ParseResult | None:
    for p in _PARSERS:
        if r := p.match(filename, folder_name=folder_name, relative_path=relative_path):
            return r
    return None


register(MultiEpisodeParser())
register(SonarrStandardParser())
register(SpecialsParser())
register(ReleaseDotParser())
register(ReleaseSpaceParser())
register(BareEpisodeParser())
register(AnihlsParser())
