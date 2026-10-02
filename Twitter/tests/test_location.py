"""Tests for the author-location filter.

The cases that matter most are the false positives. A country filter that eats
"Indiana" or "Manilla Road" is worse than no filter at all, because the leads it
removes never appear anywhere to be noticed.
"""

from __future__ import annotations

import pytest

from x_leads.leads.location import (
    DEFAULT_EXCLUDED_COUNTRIES,
    LocationFilter,
    Resolution,
    bio_country,
    countries_in_text,
    flag_countries,
    normalize_country,
    parse_country_list,
    resolve_author,
    tld_country,
)
from x_leads.models import Author, Lead, Tweet


def make_lead(**author_kwargs) -> Lead:
    return Lead(tweet=Tweet(tweet_id="1", text="need a video editor",
                            author=Author(**author_kwargs)))


# ------------------------------------------------------------ text resolution
@pytest.mark.parametrize("text,expected", [
    # Cities, which is what people actually write.
    ("Karachi", "pakistan"),
    ("Lahore, Pakistan", "pakistan"),
    ("Mumbai", "india"),
    ("Dhaka", "bangladesh"),
    ("Manila", "philippines"),
    ("Quezon City", "philippines"),
    # Longest phrase wins over the shorter one inside it.
    ("New Delhi", "india"),
    ("Navi Mumbai", "india"),
    # Country names and ISO alpha-3.
    ("India", "india"),
    ("PHL", "philippines"),
    ("Bharat", "india"),
    ("Pilipinas", "philippines"),
    # Whole-segment ISO alpha-2.
    ("Lahore, PK", "pakistan"),
    ("Cebu, PH", "philippines"),
    # Not excluded, but still resolvable — this is what makes drop_unknown work.
    ("London", "united kingdom"),
    ("Brooklyn, NY", "united states"),
    ("Dubai", "united arab emirates"),
])
def test_resolves_locations(text, expected):
    assert countries_in_text(text)[:1] == [expected]


@pytest.mark.parametrize("text", [
    # The whole reason matching is word-bounded rather than substring.
    "Indiana",
    "Indianapolis",
    "Indiana, USA",
    "Manilla Road",
    # Blank and unjudgeable values.
    "",
    "   ",
    "Worldwide",
    "Remote",
    "your DMs",
    "Earth",
    "127.0.0.1",
    # Bare alpha-2 codes must not match inside running text.
    "Made in USA",
    "The best in the world",
    "Somewhere in Germany",
])
def test_no_false_positive_for_excluded(text):
    """These must never resolve to an excluded country."""
    found = countries_in_text(text)
    assert not (set(found) & set(DEFAULT_EXCLUDED_COUNTRIES)), found


def test_unjudgeable_text_resolves_to_nothing():
    for text in ("", "Worldwide", "your DMs", "🌍"):
        assert countries_in_text(text) == []


def test_germany_mention_resolves_to_germany_not_india():
    assert countries_in_text("Somewhere in Germany") == ["germany"]


# -------------------------------------------------------------------- flags
def test_flag_emoji_decoded():
    assert flag_countries("Dhaka 🇧🇩") == ["bangladesh"]
    assert flag_countries("🇵🇰") == ["pakistan"]
    assert flag_countries("🇮🇳 creator") == ["india"]
    assert flag_countries("🇵🇭") == ["philippines"]


def test_flag_emoji_for_unexcluded_country():
    assert flag_countries("🇬🇧") == ["united kingdom"]


def test_non_flag_text_yields_no_flags():
    assert flag_countries("no flags here 🎬🔥") == []
    assert flag_countries("") == []


# ----------------------------------------------------------- weaker sources
def test_tld_country():
    assert tld_country("https://studio.com.pk") == "pakistan"
    assert tld_country("https://foo.ph/portfolio") == "philippines"
    # t.co shortlinks are what X usually hands back — no signal, not a wrong one.
    assert tld_country("https://t.co/abc123") is None
    assert tld_country("https://example.com") is None
    assert tld_country("") is None


def test_tld_ignores_ambiguous_endings():
    """`.in` and `.co` are naming conventions as often as countries."""
    assert tld_country("https://edit.in") is None
    assert tld_country("https://vids.co") is None


def test_bio_dialling_code():
    assert bio_country("Video editor. WhatsApp +92 300 1234567") == (
        "pakistan", "bio-phone"
    )
    assert bio_country("DM or call +8801712345678")[0] == "bangladesh"


def test_bio_dialling_code_needs_a_number_after_it():
    """A bare "+91" in prose is not a phone number."""
    assert bio_country("ranked +91 in the leaderboard")[0] is None
    assert bio_country("up +92 followers today")[0] is None


def test_bio_based_in():
    assert bio_country("Editor based in Lahore, open for work") == (
        "pakistan", "bio-mention"
    )
    assert bio_country("Filmmaker from Dhaka")[0] == "bangladesh"


def test_bio_city_mention_alone_is_not_a_signal():
    """Serving clients somewhere is not living there."""
    assert bio_country("I edit for creators in Mumbai and Dubai")[0] is None
    assert bio_country("Video editor | YouTube growth")[0] is None
    assert bio_country("")[0] is None


# --------------------------------------------------------- author resolution
def test_location_field_beats_weaker_sources():
    author = Author(location="London", website="https://x.com.pk", bio="+92 300")
    assert resolve_author(author) == Resolution("united kingdom", "location")


def test_falls_back_through_sources_in_order():
    assert resolve_author(Author(location="Dhaka 🇧🇩")).source == "location"
    assert resolve_author(Author(display_name="Ali 🇵🇰")) == Resolution(
        "pakistan", "flag"
    )
    assert resolve_author(Author(website="https://me.com.ph")) == Resolution(
        "philippines", "website"
    )
    assert resolve_author(Author(bio="call +91 98765 43210")) == Resolution(
        "india", "bio-phone"
    )


def test_unplaceable_author():
    resolution = resolve_author(Author(handle="someone", bio="video editor"))
    assert not resolution.known
    assert resolution.label == ""


# ------------------------------------------------------------ filter config
def test_default_list():
    f = LocationFilter.default()
    assert f.excluded == frozenset(
        {"india", "pakistan", "bangladesh", "philippines"}
    )
    assert f.mode == "report"
    # Report mode removes nothing, whatever the block list says.
    assert not f.is_active


def test_build_adds_and_removes():
    f = LocationFilter.build(add=["egypt", "NP"], remove=["india"], mode="drop")
    assert "egypt" in f.excluded
    assert "nepal" in f.excluded
    assert "india" not in f.excluded
    assert f.is_active


def test_build_from_empty_base():
    assert LocationFilter.build(base=[], add=["latvia"], mode="drop").excluded == (
        frozenset({"latvia"})
    )


def test_unknown_country_name_is_usable():
    """A country with no table entry still normalises to itself."""
    assert normalize_country("Latvia") == "latvia"
    assert normalize_country("  UAE ") == "united arab emirates"
    assert normalize_country("") is None


def test_bad_mode_rejected():
    with pytest.raises(ValueError):
        LocationFilter.build(mode="maybe")


def test_parse_country_list():
    assert parse_country_list(None) is None
    assert parse_country_list("") == []
    assert parse_country_list("off") == []
    assert parse_country_list("india, egypt") == ["india", "egypt"]


# --------------------------------------------------------------- annotation
def test_report_mode_records_but_drops_nothing():
    leads = [
        make_lead(location="Karachi"),
        make_lead(location="London"),
        make_lead(location=""),
    ]
    for lead in leads:
        lead.verdict = "warm"

    audit = LocationFilter.default(mode="report").annotate(leads)

    assert [l.location_country for l in leads] == ["Pakistan", "United Kingdom", ""]
    assert [l.location_source for l in leads] == ["location", "location", ""]
    # Counted as blocked, but nothing was actually rejected.
    assert audit["blocked"] == 1
    assert audit["dropped"] == 0
    assert all(lead.verdict == "warm" for lead in leads)
    assert audit == {**audit, "resolved": 2, "unknown": 1, "total": 3}


def test_drop_mode_rejects_with_a_reason():
    blocked, allowed = make_lead(location="Mumbai"), make_lead(location="Toronto")
    blocked.verdict = allowed.verdict = "hot"

    audit = LocationFilter.default(mode="drop").annotate([blocked, allowed])

    assert blocked.verdict == "rejected"
    assert blocked.reject_reason == "author in India"
    assert allowed.verdict == "hot"
    assert audit["dropped"] == 1


def test_score_is_preserved_on_rejection():
    """The score stays readable so a wrong drop can be spotted."""
    lead = make_lead(location="Lahore")
    lead.verdict, lead.score, lead.budget = "hot", 14, "$800"
    LocationFilter.default(mode="drop").annotate([lead])
    assert (lead.score, lead.budget) == (14, "$800")


def test_unknown_kept_by_default():
    lead = make_lead(location="")
    lead.verdict = "warm"
    LocationFilter.default(mode="drop").annotate([lead])
    assert lead.verdict == "warm"


def test_drop_unknown_when_asked():
    lead = make_lead(location="Worldwide")
    lead.verdict = "warm"
    audit = LocationFilter.default(mode="drop", drop_unknown=True).annotate([lead])
    assert lead.verdict == "rejected"
    assert lead.reject_reason == "no usable location"
    assert audit["dropped"] == 1


def test_off_mode_does_nothing_at_all():
    lead = make_lead(location="Karachi")
    lead.verdict = "hot"
    audit = LocationFilter.default(mode="off").annotate([lead])
    assert lead.verdict == "hot"
    assert lead.location_country == ""
    assert audit["resolved"] == 0


def test_untrusted_source_counts_as_unknown():
    """Narrowing trusted_sources demotes a weak signal rather than deleting it."""
    lead = make_lead(bio="WhatsApp +92 300 1234567")
    lead.verdict = "warm"
    f = LocationFilter(
        excluded=frozenset({"pakistan"}),
        mode="drop",
        trusted_sources=frozenset({"location", "flag"}),
    )
    f.annotate([lead])
    # Still recorded for auditing...
    assert lead.location_country == "Pakistan"
    # ...but not acted on.
    assert lead.verdict == "warm"


def test_audit_breakdowns():
    leads = [
        make_lead(location="Karachi"),
        make_lead(location="Lahore"),
        make_lead(location="Dhaka"),
        make_lead(display_name="Ana 🇵🇭"),
        make_lead(location="Berlin"),
        make_lead(location=""),
    ]
    audit = LocationFilter.default(mode="drop").annotate(leads)
    assert audit["by_country"] == {
        "Pakistan": 2, "Bangladesh": 1, "Philippines": 1, "Germany": 1
    }
    assert audit["by_source"] == {"location": 4, "flag": 1}
    assert audit["dropped"] == 4


# ------------------------------------------------------------------ reporting
def test_report_mode_says_what_it_would_drop():
    leads = [make_lead(location="Karachi"), make_lead(location="London")]
    f = LocationFilter.default(mode="report")
    lines = f.report(f.annotate(leads))
    text = "\n".join(lines)
    assert "2/2 authors placed" in text
    assert "would drop 1 of 2" in text
    assert "--country-filter drop" in text


def test_off_mode_reports_nothing():
    f = LocationFilter.default(mode="off")
    assert f.report(f.annotate([make_lead(location="Karachi")])) == []


def test_describe():
    assert "off" in LocationFilter.default(mode="off").describe()
    assert "Pakistan" in LocationFilter.default(mode="drop").describe()
    assert "unknown location: kept" in LocationFilter.default(mode="drop").describe()


# ------------------------------------------------------------------- output
def test_location_reaches_the_output_dict():
    lead = make_lead(location="Karachi 🇵🇰")
    LocationFilter.default(mode="report").annotate([lead])
    row = lead.to_dict()
    assert row["location"] == "Karachi 🇵🇰"
    assert row["location_country"] == "Pakistan"
    assert row["location_source"] == "location"
