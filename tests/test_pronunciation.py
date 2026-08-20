"""The one check that text-level assertions cannot make: what it sounds like.

Everything in test_textnorm asserts that a rule rewrites the right characters.
This file runs the rewritten text through Kokoro's real grapheme-to-phoneme
front end and asserts the vowel that comes out, which is the thing actually
being fixed. It is the slowest file in the suite - the tagger loads a spaCy
model once - so it stays small and targeted.
"""
from __future__ import annotations

import pytest

from server.textnorm import normalize

pytest.importorskip("misaki", reason="Kokoro's G2P is an engine dependency")

REED = "ɹˈid"      # read, present tense
RED = "ɹˈɛd"       # read, past tense
LED = "lˈɛd"       # lead, the metal
LEED = "lˈid"      # lead, the verb
SCHWA_A = "ɐ"      # a bare capital A read as the article: the "uh" in "uh eye"


@pytest.fixture(scope="session")
def phonemes():
    from misaki import en, espeak

    try:
        fallback = espeak.EspeakFallback(british=False)
    except Exception:  # noqa: BLE001 - espeak-ng absent; unknown words are dropped
        fallback = None
    g2p = en.G2P(trf=False, british=False, fallback=fallback, unk="")

    def say(text: str) -> str:
        return " ".join(g2p(chunk)[0] for chunk in normalize(text))

    return say


@pytest.mark.parametrize(
    "text, vowel",
    [
        ("They read widely every week.", REED),
        ("I read books every single day of the week.", REED),
        ("We read it now, before anybody else gets a chance.", REED),
        ("I read it yesterday, before anybody else got a chance.", RED),
        ("She has read it twice already, cover to cover.", RED),
        ("The book was read aloud to the whole class that morning.", RED),
        ("He will read it tomorrow, once the meeting is finally over.", REED),
    ],
)
def test_read_gets_the_vowel_its_tense_calls_for(phonemes, text, vowel):
    assert vowel in phonemes(text)


@pytest.mark.parametrize(
    "text, vowel",
    [
        ("The lead pipe had corroded right through at the joint.", LED),
        ("Lead paint is still a hazard in older housing stock.", LED),
        ("Inspectors found traces of lead in the drinking water.", LED),
        ("She will lead the team through the whole of next year.", LEED),
        ("The lead singer walked off stage without saying a word.", LEED),
    ],
)
def test_lead_the_metal_and_lead_the_verb_differ(phonemes, text, vowel):
    assert vowel in phonemes(text)


@pytest.mark.parametrize("initialism", ["AI", "CIA", "FAA", "NBA", "FDA"])
def test_initialisms_ending_in_a_no_longer_trail_off_into_uh(phonemes, initialism):
    """Spacing the letters made the final A the article: 'see eye uh'."""
    said = phonemes(f"The {initialism} inquiry closed without any further comment.")
    assert SCHWA_A not in said, said


def test_ai_says_ay_eye(phonemes):
    assert "ˈAˌI" in phonemes("The AI team shipped it without telling anyone first.")
