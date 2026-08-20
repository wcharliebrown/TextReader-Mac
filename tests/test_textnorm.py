"""Rules in textnorm decide how the article actually sounds, so each gets a test."""
from __future__ import annotations

from pathlib import Path

import pytest

from server.config import FIRST_CHUNK_CHARS, MAX_CHUNK_CHARS, TARGET_CHUNK_CHARS
from server.textnorm import (
    clean_text,
    disambiguate_text,
    expand_acronyms,
    expand_text,
    normalize,
    normalize_chunks,
    split_chunks,
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


def test_acronyms_are_handed_to_the_g2p_intact():
    """The G2P spells GDP and says NASA; spacing the letters only broke that."""
    assert expand_acronyms("The GDP and NASA") == "The GDP and NASA"


# -- split ------------------------------------------------------------------

def test_honorific_period_is_not_a_sentence_break():
    assert len(split_sentences("Dr. Smith arrived at the laboratory well before dawn broke.")) == 1


def test_initials_are_not_sentence_breaks():
    chunks = split_sentences("The J. R. R. Tolkien estate sued a publisher in the year after.")
    assert len(chunks) == 1


def test_real_sentence_breaks_are_found():
    """Boundary detection, isolated from packing by a target of one character."""
    text = (
        "The first sentence runs on for a while so it clears the merge threshold. "
        "The second sentence also runs on for a while and clears it too."
    )
    chunks = split_chunks(text, min_chars=0, target_chars=1, first_chars=1)
    assert [c for c, _ in chunks] == [
        "The first sentence runs on for a while so it clears the merge threshold.",
        "The second sentence also runs on for a while and clears it too.",
    ]


def test_short_fragments_are_merged():
    # Standalone one- and two-word chunks sound clipped; they must not survive.
    assert all(len(c) >= 40 or len(c.split()) > 3 for c in split_sentences("Ok. Yes. No. Fine."))


def test_long_sentence_is_split_at_clause_boundaries():
    long = "This clause runs on, " * 30
    chunks = split_sentences(long, max_chars=300)
    assert chunks and all(len(c) <= 300 for c in chunks)


def test_no_chunk_exceeds_the_length_kokoro_renders_in_one_pass():
    """Past ~500 characters Kokoro splits internally and bakes in its own seam."""
    long = "This clause runs on and on, " * 60
    assert all(len(c) <= MAX_CHUNK_CHARS < 500 for c in split_sentences(long))


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


# --------------------------------------------------------------------------
# Regressions: abbreviations matching inside words, and shouted words.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("She was the best.", "best."),
        ("That is the honest.", "honest."),
        ("A hard test.", "test."),
        ("It rose in revs.", "revs."),
        ("The forest.", "forest."),
    ],
)
def test_abbreviation_does_not_match_inside_a_word(text, expected):
    """'est.' inside 'best.' produced 'bestimated'; 'vs.' inside 'revs.' produced 'reversus'."""
    assert expand_text(text).endswith(expected)


def test_abbreviation_still_expands_when_it_is_one():
    assert expand_text("The est. cost") == "The estimated cost"
    assert expand_text("Ali vs. Frazier") == "Ali versus Frazier"


def test_abbreviation_inside_a_word_no_longer_swallows_the_sentence_break():
    """The period must survive; packing may then place both in one chunk."""
    spoken = " ".join(normalize(
        "By every reasonable measure that answer was the honest. "
        "Truth follows right here in a whole second sentence."
    ))
    assert "the honest. Truth" in spoken, spoken


@pytest.mark.parametrize("shout", ["HUGE", "STOP", "FREE", "BEST"])
def test_shouted_real_words_are_not_spelled_out(shout):
    """'a HUGE deal' used to read as 'a H U G E deal'."""
    assert expand_acronyms(f"a {shout} deal") == f"a {shout.lower()} deal"


@pytest.mark.parametrize("initialism", ["CIA", "FBI", "IMF", "HTTP", "FAA", "AI"])
def test_initialisms_are_left_for_the_g2p_to_spell(initialism):
    """Our own spacing turned a trailing letter A into the article: 'see eye uh'."""
    assert expand_acronyms(f"the {initialism} file") == f"the {initialism} file"


@pytest.mark.parametrize("word_acronym", ["NASA", "COVID", "SCUBA"])
def test_acronyms_pronounced_as_words_are_left_alone(word_acronym):
    assert expand_acronyms(f"the {word_acronym} file") == f"the {word_acronym} file"


@pytest.mark.parametrize(
    "acronym, spoken", [("JSON", "Jason"), ("YAML", "yammel"), ("IKEA", "Ikea")]
)
def test_the_few_the_g2p_gets_wrong_are_respelled(acronym, spoken):
    """Left alone these three come out as strings of letters, not as words."""
    assert expand_acronyms(f"a {acronym} file") == f"a {spoken} file"


@pytest.mark.parametrize("ambiguous", ["US", "IT", "AI", "SEC"])
def test_initialisms_that_are_also_words_keep_their_capitals(ambiguous):
    """'the US' must not degrade to 'the us' via the dictionary check."""
    assert expand_acronyms(f"the {ambiguous} team") == f"the {ambiguous} team"


def test_word_list_falls_back_when_the_system_dictionary_is_missing(monkeypatch):
    import server.textnorm as tn

    tn._english_words.cache_clear()
    monkeypatch.setattr(tn, "_WORD_LIST", Path("/nonexistent/words"))
    try:
        assert "best" in tn._english_words()
        assert tn.expand_acronyms("a HUGE deal") == "a huge deal"
    finally:
        tn._english_words.cache_clear()


# --------------------------------------------------------------------------
# Chunk packing: whole sentences grouped so Kokoro carries intonation across a
# full stop, with only the first chunk kept short for time-to-first-audio.
# --------------------------------------------------------------------------

SENTENCE = "This sentence is a fairly ordinary length for prose in an article. "


def test_only_the_first_chunk_is_held_short():
    chunks = split_sentences(SENTENCE * 12)
    assert len(chunks) > 2, "nothing to compare"
    assert len(chunks[0]) <= FIRST_CHUNK_CHARS
    assert any(len(c) > FIRST_CHUNK_CHARS for c in chunks[1:]), "later chunks were not packed"


def test_packed_chunks_group_several_sentences():
    packed = split_sentences(SENTENCE * 12)[1]
    assert packed.count(".") > 1, f"one sentence per chunk defeats the point: {packed!r}"
    assert len(packed) <= TARGET_CHUNK_CHARS


def test_packing_never_crosses_a_paragraph():
    chunks = normalize_chunks("First paragraph here.\n\nSecond paragraph here.")
    assert [c for c, _ in chunks] == ["First paragraph here.", "Second paragraph here."]


def test_last_chunk_of_each_paragraph_is_flagged():
    text = "\n\n".join([SENTENCE * 6, SENTENCE * 6])
    flags = [para_end for _, para_end in normalize_chunks(text)]
    assert flags[-1] is True
    assert flags.count(True) == 2, f"expected one flag per paragraph, got {flags}"
    assert False in flags, "a multi-chunk paragraph should have unflagged chunks"


def test_a_tiny_trailing_fragment_still_merges():
    assert split_sentences("A perfectly ordinary opening sentence of decent length. Yes.") == [
        "A perfectly ordinary opening sentence of decent length. Yes."
    ]


# --------------------------------------------------------------------------
# Heteronyms: words the G2P would otherwise read with the wrong vowel. Only the
# cases measured wrong are listed; "live", "wind", "record" and the rest are
# deliberately absent because the G2P already gets them right.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Present tense is the bug: the tagger calls it VBP and the lexicon
        # maps VBP to the past-tense sound.
        ("They read widely.", "They reed widely."),
        ("I read books every day.", "I reed books every day."),
        ("We read it now.", "We reed it now."),
        ("You read too fast.", "You reed too fast."),
        ("people read less than they claim", "people reed less than they claim"),
        ("They never read it.", "They never reed it."),
        # A past-tense cue anywhere in the sentence cancels that.
        ("I read it yesterday.", "I read it yesterday."),
        ("We read it last week.", "We read it last week."),
        ("They read it two years ago.", "They read it two years ago."),
        # The perfect and the passive are pinned so a chunk boundary cannot
        # strip the auxiliary that makes them right.
        ("She has read it.", "She has red it."),
        ("The book was read aloud.", "The book was red aloud."),
        ("a widely read author", "a widely red author"),
        # Untouched: the infinitive and the modal already say it correctly.
        ("He will read it.", "He will read it."),
        ("Please read the book.", "Please read the book."),
    ],
)
def test_read_is_respelled_by_context(raw, expected):
    assert disambiguate_text(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        # The lexicon has no part-of-speech variants for "lead", so the metal
        # is wrong every single time.
        ("The lead pipe.", "The led pipe."),
        ("Lead paint is toxic.", "Led paint is toxic."),
        ("lead poisoning", "led poisoning"),
        ("traces of lead in the water", "traces of led in the water"),
        ("blood lead levels", "blood led levels"),
        # The verb and the noun are right already and must stay put.
        ("She will lead the team.", "She will lead the team."),
        ("the lead singer", "the lead singer"),
        ("a sales lead", "a sales lead"),
        ("a lead role", "a lead role"),
        # "leaded" is a different word and the G2P knows it.
        ("leaded gasoline", "leaded gasoline"),
    ],
)
def test_lead_the_metal_is_respelled(raw, expected):
    assert disambiguate_text(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("striped bass", "striped base"),
        ("bass fishing", "base fishing"),
        ("the bass guitar", "the bass guitar"),
        ("a bass line", "a bass line"),
        ("his resume", "his r\u00e9sum\u00e9"),
        ("the resumes piled up", "the r\u00e9sum\u00e9s piled up"),
        ("they resume work", "they resume work"),
    ],
)
def test_other_measured_heteronyms(raw, expected):
    assert disambiguate_text(raw) == expected


def test_respelling_keeps_the_original_capitalisation():
    assert disambiguate_text("Lead poisoning spreads.") == "Led poisoning spreads."


def test_heteronyms_are_fixed_by_the_time_the_pipeline_finishes():
    out = spoken("The lead pipe leaked, and they read about it in the paper.")
    assert "led pipe" in out
    assert "they reed about" in out


def test_ai_is_no_longer_read_as_uh_eye():
    """Spaced out, 'A I' put the article in front of the letter I."""
    assert spoken("The AI team shipped it.") == "The AI team shipped it."
