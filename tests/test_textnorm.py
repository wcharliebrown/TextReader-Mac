"""Rules in textnorm decide how the article actually sounds, so each gets a test."""
from __future__ import annotations

import pytest

from server.textnorm import (
    clean_text,
    expand_acronyms,
    expand_text,
    normalize,
    split_sentences,
)


def spoken(text: str) -> str:
    return " ".join(normalize(text))


# -- clean ------------------------------------------------------------------

def test_strips_citation_markers():
    assert "[1]" not in clean_text("A claim.[1] Another.[citation needed]")
    assert clean_text("A claim.[1] Another.") == "A claim. Another."


def test_rejoins_hyphenated_line_break():
    assert clean_text("narra-\ntive") == "narrative"


def test_removes_zero_width_and_soft_hyphen():
    assert clean_text("para­graph​") == "paragraph"


def test_normalises_smart_punctuation():
    assert clean_text("“quoted” — it’s") == '"quoted" - it\'s'


def test_drops_nav_furniture_lines():
    assert "Advertisement" not in clean_text("Real prose.\nAdvertisement\nMore prose.")


def test_deshouts_allcaps_headline():
    assert clean_text("BREAKING NEWS IN LONDON") == "Breaking news in london"


def test_keeps_short_allcaps_line_intact():
    # Two words is an acronym pair, not a shouted headline.
    assert clean_text("NASA IMF") == "NASA IMF"


# -- expand -----------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("$1.2M", "one point two million dollars"),
        ("$500,000", "five hundred thousand dollars"),
        ("$1", "one dollar"),
        ("£3bn", "three billion pounds"),
        ("3%", "three percent"),
        ("2.5%", "two point five percent"),
        ("41°C", "forty-one degrees Celsius"),
        ("5km", "five kilometres"),
        ("300 kph", "three hundred kilometres per hour"),
        ("1st", "first"),
        ("22nd", "twenty-second"),
    ],
)
def test_expansions(raw, expected):
    assert expand_text(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("in 1995.", "in nineteen ninety-five."),   # trailing period must not block it
        ("in 1995,", "in nineteen ninety-five,"),   # nor a trailing comma
        ("in 2021 ", "in twenty twenty-one "),
        ("2010-2015", "twenty ten to twenty fifteen"),
    ],
)
def test_years_read_as_years(raw, expected):
    assert expand_text(raw) == expected


def test_thousands_separator_is_not_a_year():
    assert expand_text("1,995 items") == "one thousand nine hundred ninety-five items"


def test_number_words_have_no_commas_or_british_and():
    out = expand_text("1,234")
    assert "," not in out and " and " not in out


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Dr. Smith", "Doctor Smith"),
        ("i.e. fast", "that is fast"),
        ("approx. ten", "approximately ten"),
        ("etc.", "et cetera"),
    ],
)
def test_abbreviations(raw, expected):
    assert expand_text(raw) == expected


def test_urls_become_spoken_domains():
    assert expand_text("See https://www.example.com/a/b now") == "See www dot example dot com now"


def test_acronyms_spelled_out_but_words_left_alone():
    assert expand_acronyms("The GDP and NASA") == "The G D P and NASA"


# -- split ------------------------------------------------------------------

def test_honorific_period_is_not_a_sentence_break():
    assert len(split_sentences("Dr. Smith arrived at the laboratory well before dawn broke.")) == 1


def test_initials_are_not_sentence_breaks():
    chunks = split_sentences("The J. R. R. Tolkien estate sued a publisher in the year after.")
    assert len(chunks) == 1


def test_real_sentence_breaks_are_found():
    text = (
        "The first sentence runs on for a while so it clears the merge threshold. "
        "The second sentence also runs on for a while and clears it too."
    )
    assert len(split_sentences(text)) == 2


def test_short_fragments_are_merged():
    # Standalone one- and two-word chunks sound clipped; they must not survive.
    assert all(len(c) >= 40 or len(c.split()) > 3 for c in split_sentences("Ok. Yes. No. Fine."))


def test_long_sentence_is_split_at_clause_boundaries():
    long = "This clause runs on, " * 30
    chunks = split_sentences(long)
    assert chunks and all(len(c) <= 300 for c in chunks)


def test_split_never_loses_or_duplicates_words():
    text = "Alpha beta gamma delta epsilon. Zeta eta theta iota kappa lambda mu nu xi."
    joined = " ".join(split_sentences(text))
    assert joined.split() == text.split()


def test_paragraphs_do_not_merge_across_the_break():
    chunks = split_sentences("A first paragraph that is easily long enough.\n\nA second one, also long enough.")
    assert len(chunks) == 2


# -- end to end -------------------------------------------------------------

def test_full_pipeline_on_a_messy_passage():
    out = spoken("Dr. Smith raised $1.2M in 1995, i.e. approx. 3% of the fund.[1]")
    assert "Doctor Smith" in out
    assert "one point two million dollars" in out
    assert "nineteen ninety-five" in out
    assert "that is approximately" in out
    assert "three percent" in out
    assert "[1]" not in out


def test_empty_input_yields_no_chunks():
    assert normalize("   \n\n  ") == []
