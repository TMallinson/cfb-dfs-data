from __future__ import annotations

from pathlib import Path

from cfb_dfs.models import Team
from cfb_dfs.models.slates import DkTeam
from cfb_dfs.transform.teams import Overrides, TeamIndex, normalize

REPO = Path(__file__).resolve().parents[1]


def _index(load_json) -> TeamIndex:
    teams = [Team.model_validate(t) for t in load_json("cfbd_teams_subset.json")]
    return TeamIndex(teams, Overrides.load(REPO / "data/overrides/team_aliases.yaml"))


def test_normalize():
    assert normalize("Texas A&M") == "texas a m"
    assert normalize("Hawai'i") == "hawaii"
    assert normalize("Miami (FL)") == "miami fl"


def test_dk_full_name_matches(load_json):
    idx = _index(load_json)
    t = idx.match_dk(DkTeam(city="Penn State", nickname="Nittany Lions", abbreviation="PSU"))
    assert t is not None and t.school == "Penn State"
    t = idx.match_dk(DkTeam(city="Texas A&M", nickname="Aggies", abbreviation="TA&M"))
    assert t is not None and t.school == "Texas A&M"


def test_dk_abbreviation_overrides(load_json):
    idx = _index(load_json)
    assert (
        idx.match_dk(DkTeam(city="Rutgers", nickname="Scarlet Knights", abbreviation="RU")).school
        == "Rutgers"
    )
    assert (
        idx.match_dk(DkTeam(city="Alabama", nickname="Crimson Tide", abbreviation="BAMA")).school
        == "Alabama"
    )
    assert (
        idx.match_dk(DkTeam(city="Hawaii", nickname="Rainbow Warriors", abbreviation="HAW")).school
        == "Hawai'i"
    )


def test_unmatched_is_recorded_not_guessed(load_json):
    idx = _index(load_json)
    assert idx.match_dk(DkTeam(city="Nowhere", nickname="Nobodies", abbreviation="NOW")) is None
    assert idx.unmatched == ["Nowhere Nobodies (NOW)"]


def test_display_abbreviation_prefers_dk(load_json):
    idx = _index(load_json)
    bama = idx.by_school["Alabama"]
    assert idx.abbr(bama.id) == "ALA"  # CFBD default
    idx.adopt_dk_abbreviations([(bama.id, "BAMA")])
    assert idx.abbr(bama.id) == "BAMA"
    assert idx.is_fbs(bama.id) and not idx.is_fbs(idx.by_school["Florida A&M"].id)
