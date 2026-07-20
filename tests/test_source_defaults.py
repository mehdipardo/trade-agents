"""Source catalog defaults: pre-filled config + required-only needs_config."""

from __future__ import annotations

from app.sources import catalog


def _spec(sources: list[dict], sid: str) -> dict:
    return next(s for s in sources if s["id"] == sid)


def test_truth_social_ships_with_trump_family_watchlist() -> None:
    sources = catalog.as_dict()
    ts = _spec(sources, "trump_truthsocial")
    watchlist = next(f for f in ts["config_fields"] if f["name"] == "truth_social_urls")
    assert "@realDonaldTrump" in watchlist["value"]
    assert "@DonaldJTrumpJr" in watchlist["value"]
    assert watchlist["has_value"] is True


def test_truth_social_not_flagged_needs_setup_by_default() -> None:
    # The only required field (watchlist) has a default -> configured out of box.
    ts = _spec(catalog.as_dict(), "trump_truthsocial")
    assert ts["needs_config"] is False


def test_optional_fields_do_not_trigger_needs_config() -> None:
    ts = _spec(catalog.as_dict(), "trump_truthsocial")
    token = next(f for f in ts["config_fields"] if f["name"] == "truth_social_token")
    assert token["required"] is False
    assert token["has_value"] is False  # empty, but optional -> no "needs setup"


def test_operator_override_wins_over_default() -> None:
    sources = catalog.as_dict(
        current_values={"trump_truthsocial": {"truth_social_urls": "@custom"}}
    )
    watchlist = next(
        f for f in _spec(sources, "trump_truthsocial")["config_fields"]
        if f["name"] == "truth_social_urls"
    )
    assert watchlist["value"] == "@custom"


def test_congress_still_needs_setup_no_free_default() -> None:
    # A source whose required fields have no default still reports needs_config.
    congress = _spec(catalog.as_dict(), "congress_bills")
    assert congress["needs_config"] is True
