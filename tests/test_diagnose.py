"""The pure half of the round-trip diagnostic: canonicalisation and alignment.

The transcription half needs a Whisper model and a running server, so it is
exercised by hand (python -m server.diagnose); what is testable here is that
the diff only fires on genuine disagreements, not on spelling.
"""
from server.diagnose import align, canon_words


def test_digits_and_words_meet_in_the_middle():
    # Whisper writes numbers back as digits; both sides expand to the same words.
    assert canon_words("It costs $4.5m.") == canon_words(
        "it costs four point five million dollars"
    )
    assert canon_words("at 3:30 p.m.")[-3:] == ["thirty", "p", "m"]


def test_punctuation_case_and_hyphens_do_not_count():
    assert canon_words("Lead-free pipe!") == canon_words("lead free pipe")
    assert canon_words('"Hello," she said.') == ["hello", "she", "said"]


def test_titles_match_their_spoken_form():
    # textnorm expands "Dr." before synthesis; Whisper usually writes it back
    # abbreviated. Both must canonicalise to the spoken word.
    assert canon_words("Dr. Smith") == canon_words("Doctor Smith")


def test_identical_texts_align_clean():
    words = canon_words("The quick brown fox jumps over the lazy dog.")
    assert align(words, words) == []


def test_a_dropped_word_is_reported():
    said = canon_words("the toxic lead pipe burst")
    heard = canon_words("the toxic pipe burst")
    assert align(said, heard) == [("delete", "lead", "")]


def test_a_substituted_word_is_reported():
    said = canon_words("briefed on a Jason payload")
    heard = canon_words("briefed on a jaysawn payload")
    (op, want, got), = align(said, heard)
    assert (op, want, got) == ("replace", "jason", "jaysawn")


def test_apostrophes_inside_words_survive():
    assert canon_words("nine o'clock") == ["nine", "o'clock"]
    assert canon_words("don't stop") == ["don't", "stop"]


def test_our_respellings_coming_back_in_dictionary_form_are_not_flagged():
    # Whisper hears the right sound and writes the conventional spelling;
    # that is the round trip succeeding, so the diff must stay quiet.
    said = canon_words("the led pipe and a Jason payload with a yammel config")
    heard = canon_words("The lead pipe and a JSON payload with a YAML config.")
    assert align(said, heard) == []


def test_accents_and_british_spellings_are_not_flagged():
    assert align(canon_words("his résumé"), canon_words("his resume")) == []
    assert align(canon_words("30km of pipe"), canon_words("30 kilometers of pipe")) == []


def test_compact_money_matches_its_spoken_order():
    assert align(
        canon_words("four point five million dollars"), canon_words("$4.5 million")
    ) == []


def test_a_genuinely_wrong_word_still_gets_through_the_whitelist():
    said = canon_words("the led pipe")
    heard = canon_words("the light pipe")
    assert align(said, heard) == [("replace", "led", "light")]


def test_transcribed_times_and_dates_are_not_flagged():
    # Whisper writes "3.30 PM" for a spoken "three thirty P M", and writes a
    # spoken "twelve slash five" back as a bare date.
    assert align(canon_words("at three thirty P M"), canon_words("at 3.30 PM.")) == []
    assert align(
        canon_words("on twelve slash five slash twenty twenty-five"),
        canon_words("on 12-5-2025"),
    ) == []
