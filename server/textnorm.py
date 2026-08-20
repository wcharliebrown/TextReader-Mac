"""Turn messy web text into clean, speakable sentences.

Four pure stages so every rule is unit-testable. The only I/O is a cached,
best-effort read of the system word list used to tell an emphasised word
(HUGE) from an initialism (CIA); it degrades to a built-in list if absent.

    clean_text        strip the junk that copy-pasting from a web page drags along
    disambiguate_text respell heteronyms the G2P would otherwise read the wrong way
    expand_text       rewrite symbols, numbers and abbreviations as spoken words
    split_sentences   cut into chunks sized for low-latency streaming synthesis

`normalize` runs all four.
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from num2words import num2words

from .config import (
    FIRST_CHUNK_CHARS,
    MAX_CHUNK_CHARS,
    MIN_CHUNK_CHARS,
    TARGET_CHUNK_CHARS,
)

# --------------------------------------------------------------------------
# clean
# --------------------------------------------------------------------------

# Zero-width and other invisible characters that survive a copy-paste.
_INVISIBLE = re.compile(r"[­​-‏  ﻿⁠]")
_SMART_MAP = str.maketrans(
    {
        "‘": "'", "’": "'", "‚": "'", "‛": "'",
        "“": '"', "”": '"', "„": '"',
        "–": "-", "—": "-", "―": "-", "−": "-",
        "…": "...", " ": " ", " ": " ", " ": " ",
        "•": " ", "·": " ",
    }
)
# Word broken across a line by a hyphen: "narra-\ntive" -> "narrative".
_LINEBREAK_HYPHEN = re.compile(r"(\w)-\s*\n\s*(\w)")
# Citation and footnote markers: [1], [12], [citation needed], [a].
_CITATION = re.compile(r"\[\s*(?:\d{1,3}|[a-z]|citation needed|note \d+)\s*\]", re.I)
_FOOTNOTE_MARK = re.compile(r"[†‡§¶∗]")
# Leftover markdown emphasis / heading marks.
_MD_NOISE = re.compile(r"(?<!\w)[*_#`]{1,3}(?!\w)")
_MULTI_NEWLINE = re.compile(r"\n{2,}")
_SPACES = re.compile(r"[ \t]+")

# Lone fragments that are page furniture rather than prose.
_NAV_JUNK = re.compile(
    r"^(?:share|tweet|advertisement|advertise|subscribe|sign in|log in|menu|skip to content"
    r"|read more|related|comments?|\d+ min read|photo|image|credit|getty images?)$",
    re.I,
)


def _unshout(line: str) -> str:
    """Lower-case a shouted headline so it is not read out letter by letter."""
    words = [w for w in line.split() if any(c.isalpha() for c in w)]
    if len(words) < 3:
        return line
    caps = sum(1 for w in words if w.isupper())
    if caps / len(words) < 0.8:
        return line
    return line[:1] + line[1:].lower()


def clean_text(text: str) -> str:
    """Strip copy-paste artefacts, citation markers and page furniture."""
    text = unicodedata.normalize("NFKC", text)
    text = _INVISIBLE.sub("", text)
    text = text.translate(_SMART_MAP)
    text = _LINEBREAK_HYPHEN.sub(r"\1\2", text)
    text = _CITATION.sub("", text)
    text = _FOOTNOTE_MARK.sub("", text)
    text = _MD_NOISE.sub("", text)

    lines = [_SPACES.sub(" ", ln).strip() for ln in text.split("\n")]
    lines = ["" if _NAV_JUNK.match(ln) else _unshout(ln) for ln in lines]
    text = "\n".join(lines)

    # Collapse blank runs to a single paragraph break, keep single newlines
    # as soft breaks so the splitter can see paragraph structure.
    text = _MULTI_NEWLINE.sub("\n\n", text)
    return text.strip()


# --------------------------------------------------------------------------
# disambiguate
# --------------------------------------------------------------------------

# Kokoro's G2P chooses a heteronym's sound from a part-of-speech tag, and it is
# right far more often than not: "live", "wind", "tear", "record", "present",
# "desert" and two dozen others need no help here. What follows is only the set
# measured wrong, respelled as ordinary words the G2P already says correctly.
# Respelling rather than phoneme markup keeps this stage engine-independent -
# and a respelling has to be a real word, because anything else falls through
# to the espeak fallback and comes out unpredictable.

_SENT_EDGE = re.compile(r"[.!?\n]")


def _sentence_around(text: str, start: int, end: int, radius: int = 200) -> str:
    """The clause a match sits in, so a rule can look at its whole sentence."""
    head = text[max(0, start - radius) : start]
    head = head[max(head.rfind(c) for c in ".!?\n") + 1 :]
    tail = text[end : end + radius]
    edge = _SENT_EDGE.search(tail)
    return head + text[start:end] + (tail[: edge.end()] if edge else tail)


def _like(word: str, replacement: str) -> str:
    """Carry the original word's capitalisation onto its respelling."""
    return replacement.capitalize() if word[:1].isupper() else replacement


# Anything that pins a bare "read" to the past. Without one of these, "they
# read" is taken as present tense - which is the reading the G2P gets wrong.
_PAST_CUE = re.compile(
    r"\b(?:yesterday|ago|earlier|previously|recently|formerly|once|then"
    r"|last\s+(?:night|week|month|year|time|century|summer|winter|spring|autumn)"
    r"|back\s+in|in\s+(?:1[5-9]\d{2}|20\d{2}))\b",
    re.I,
)

_ADVERB = r"(?:never|always|often|rarely|seldom|sometimes|usually|still|also|only|already|\w+ly)"

# (pattern, respelling, blocker) - the pattern must capture the ambiguous word
# as `w`; the blocker, when given, cancels the rule if it matches the sentence.
_HETERONYMS: list[tuple[re.Pattern[str], str, re.Pattern[str] | None]] = [
    # "read": the G2P is already right for the perfect and the passive, but a
    # chunk can begin after the auxiliary that made it right, so pin it while
    # the whole sentence is still in one piece.
    (
        re.compile(
            r"\b(?:have|has|had|having|been|being|was|were|is|are|get|gets|got)"
            r"(?:\s+" + _ADVERB + r")?\s+(?P<w>read)\b",
            re.I,
        ),
        "red",
        None,
    ),
    (re.compile(r"\b(?:well|widely|much|little|barely|hardly)[\s-]+(?P<w>read)\b", re.I), "red", None),
    # The actual bug: a non-third-person present "read" is tagged VBP, and the
    # lexicon maps VBP to the past-tense sound.
    (
        re.compile(
            r"\b(?:I|we|you|they|who|people)"
            r"(?:\s+" + _ADVERB + r")?\s+(?P<w>read)\b",
            re.I,
        ),
        "reed",
        _PAST_CUE,
    ),
    # "lead": the lexicon has no part-of-speech variants at all, so the metal is
    # always wrong and the verb is always right. Only the metal needs a rule.
    (
        re.compile(
            r"\b(?P<w>lead)[\s-](?:poisoning|paint|pipes?|piping|solder|acetate|shot"
            r"|bullets?|weights?|dust|ore|smelter|glass|crystal|apron|foil"
            r"|contamination|levels?|exposure|batter(?:y|ies))\b",
            re.I,
        ),
        "led",
        None,
    ),
    (
        re.compile(
            r"\b(?:tetraethyl|molten|toxic|blood|leached|leaching"
            r"|traces?\s+of|levels?\s+of|amounts?\s+of|parts?\s+of"
            r"|contains?|contained|containing)\s+(?P<w>lead)\b",
            re.I,
        ),
        "led",
        None,
    ),
    # "bass": the instrument is right, the fish is not.
    (re.compile(r"\b(?:sea|striped|largemouth|smallmouth)\s+(?P<w>bass)\b", re.I), "base", None),
    (re.compile(r"\b(?P<w>bass)\s+(?:fish\w*|boat)\b", re.I), "base", None),
    # "resume" the noun reads as the verb; the accented spelling is in the
    # lexicon and says it properly.
    (
        re.compile(
            r"\b(?:an?|the|his|her|their|my|your|its|updated?|submitte?d?|attached?|polished?)"
            r"\s+(?P<w>resume)\b",
            re.I,
        ),
        "r\u00e9sum\u00e9",
        None,
    ),
    (
        re.compile(r"\b(?:the|their|our|your|these|those|\d+)\s+(?P<w>resumes)\b", re.I),
        "r\u00e9sum\u00e9s",
        None,
    ),
]


def disambiguate_text(text: str) -> str:
    """Respell words whose spelling does not settle how they are said.

    Runs before expansion, so past-tense cues are still written as digits, and
    before splitting, because a chunk that starts mid-sentence strips away the
    very context these rules read.
    """
    for pattern, replacement, blocker in _HETERONYMS:

        def swap(m: re.Match[str], replacement: str = replacement, blocker=blocker) -> str:
            if blocker and blocker.search(_sentence_around(text, m.start(), m.end())):
                return m.group(0)
            head, tail = m.start(), m.start("w")
            return (
                m.group(0)[: tail - head]
                + _like(m.group("w"), replacement)
                + m.group(0)[m.end("w") - head :]
            )

        text = pattern.sub(swap, text)
    return text


# --------------------------------------------------------------------------
# expand
# --------------------------------------------------------------------------

_CURRENCY_WORDS = {"$": "dollars", "£": "pounds", "€": "euros", "¥": "yen", "₹": "rupees"}
_CURRENCY_SINGULAR = {"$": "dollar", "£": "pound", "€": "euro", "¥": "yen", "₹": "rupee"}
_MAGNITUDE = {
    "k": "thousand", "m": "million", "bn": "billion", "b": "billion",
    "tn": "trillion", "t": "trillion",
}

_UNITS = {
    "km": "kilometres", "cm": "centimetres", "mm": "millimetres", "nm": "nanometres",
    "kg": "kilograms", "mg": "milligrams", "lb": "pounds", "lbs": "pounds",
    "ms": "milliseconds", "hz": "hertz", "khz": "kilohertz", "mhz": "megahertz",
    "ghz": "gigahertz", "kb": "kilobytes", "mb": "megabytes", "gb": "gigabytes",
    "tb": "terabytes", "kw": "kilowatts", "mw": "megawatts", "gw": "gigawatts",
    "mph": "miles per hour", "kph": "kilometres per hour", "ft": "feet",
}

_ABBREVIATIONS = {
    "Dr.": "Doctor", "Mr.": "Mister", "Mrs.": "Missus", "Ms.": "Miz",
    "Prof.": "Professor", "St.": "Saint", "Mt.": "Mount", "Rev.": "Reverend",
    "Gen.": "General", "Sen.": "Senator", "Rep.": "Representative", "Gov.": "Governor",
    "e.g.": "for example", "i.e.": "that is", "etc.": "et cetera",
    "vs.": "versus", "cf.": "compare", "approx.": "approximately",
    "est.": "estimated", "incl.": "including", "dept.": "department",
    "Inc.": "Incorporated", "Ltd.": "Limited", "Corp.": "Corporation",
    "Co.": "Company", "Jr.": "Junior", "Sr.": "Senior",
    "a.m.": "A M", "p.m.": "P M", "U.S.": "US", "U.K.": "UK", "E.U.": "EU",
}
# The lookbehind is load-bearing: without it "est." matches inside "best.",
# producing "bestimated", and "vs." matches inside "revs.".
_ABBREV_RE = re.compile(
    r"(?<![A-Za-z])(?:"
    + "|".join(re.escape(k) for k in sorted(_ABBREVIATIONS, key=len, reverse=True))
    + r")"
)

_WORD_LIST = Path("/usr/share/dict/words")

# Enough of the common emphasis words to stay useful where the system list is
# missing. Anything absent is spelled out, which is the old behaviour.
_FALLBACK_WORDS = frozenset("""
best rest test west east most must just huge stop free love hate need want
help wait care fast slow real true good bad big new now yes not all any
never ever only very much more less same next last full open shut safe sure
done gone here there this that then them they what when where why how who
work hard easy soon late long short high low hot cold dead live loud
""".split())


@lru_cache(maxsize=1)
def _english_words() -> frozenset[str]:
    """Lower-cased system dictionary, or a small built-in list if unavailable."""
    try:
        text = _WORD_LIST.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return _FALLBACK_WORDS
    return frozenset(w.strip().lower() for w in text.splitlines() if w.strip())


# Initialisms whose lower-case form is an ordinary English word. Without this
# list the dictionary check below would flatten "the US" to "the us", and the
# G2P needs the capitals to know it is looking at letters and not a word.
_KEEP_CAPS = {
    "US", "USA", "UK", "EU", "UN", "IT", "AI", "AR", "VR", "ID", "PC", "TV",
    "CD", "DVD", "HR", "PR", "IP", "OS", "UI", "UX", "EV", "AP", "ATM", "SEC",
    "WHO", "GPS", "USB", "CEO", "CFO", "CTO", "GDP", "FBI", "CIA", "IRS", "FDA",
    "AWS", "API", "CPU", "GPU", "SSD", "HTML", "CSS", "HTTP", "URL", "MRI",
}

# All-caps strings that are pronounced as words, not spelled out.
_WORD_ACRONYMS = {
    "NASA", "NATO", "AIDS", "SCUBA", "LASER", "RADAR", "UNESCO", "UNICEF", "OPEC",
    "SWAT", "RAM", "ROM", "PIN", "GIF", "JPEG", "ASCII", "SQL", "WIFI", "ZIP",
    "OK", "AM", "PM", "SAAB", "FIFA", "NAFTA", "SIM", "MIDI",
    "CAPTCHA", "MOOC", "COVID", "SARS", "PDF", "GIS", "LIDAR", "CRISPR",
}

# The G2P says these as strings of letters when they are meant to be words.
# Measured: everything else it was given intact came out right, these did not.
_RESPELL_ACRONYM = {"JSON": "Jason", "YAML": "yammel", "IKEA": "Ikea"}

_URL = re.compile(r"\b(?:https?://|www\.)([\w.-]+\.[a-z]{2,})(?:/[^\s]*)?", re.I)
_EMAIL = re.compile(r"\b[\w.+-]+@([\w-]+\.[\w.-]+)\b")
_CURRENCY_MAG = re.compile(r"([$£€¥₹])\s?(\d[\d,]*(?:\.\d+)?)\s?(k|m|bn|b|tn|t)\b", re.I)
_CURRENCY = re.compile(r"([$£€¥₹])\s?(\d[\d,]*(?:\.\d+)?)")
_PERCENT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s?%")
_DEGREES = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s?°\s?([CFK])\b")
_UNIT = re.compile(
    r"\b(\d[\d,]*(?:\.\d+)?)\s?(" + "|".join(sorted(_UNITS, key=len, reverse=True)) + r")\b",
    re.I,
)
_ORDINAL = re.compile(r"\b(\d+)(st|nd|rd|th)\b", re.I)
_YEAR_RANGE = re.compile(r"\b(1[5-9]\d{2}|20\d{2})\s?-\s?(1[5-9]\d{2}|20\d{2})\b")
_YEAR = re.compile(r"(?<![\d.,])(1[5-9]\d{2}|20\d{2})(?!\d|[.,]\d)")
_NUMBER = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)(?![\w])")
_ACRONYM = re.compile(r"\b([A-Z]{2,5})\b")


def _tidy_number_words(words: str) -> str:
    """num2words emits 'one thousand, nine hundred and ninety-five'.

    The comma becomes an audible stumble and the 'and' is British; an American
    voice reads 'nineteen ninety-five' style output far more naturally.
    """
    return re.sub(r"\s+", " ", words.replace(",", " ").replace(" and ", " ")).strip()


def _say_number(raw: str) -> str:
    """Read a plain number: '1,234' -> 'one thousand two hundred thirty-four'."""
    raw = raw.replace(",", "")
    try:
        if "." in raw:
            whole, frac = raw.split(".", 1)
            head = _tidy_number_words(num2words(int(whole))) if whole else "zero"
            digits = " ".join(num2words(int(d)) for d in frac)
            return f"{head} point {digits}"
        return _tidy_number_words(num2words(int(raw)))
    except (ValueError, OverflowError):
        return raw


def _say_year(raw: str) -> str:
    try:
        return _tidy_number_words(num2words(int(raw), to="year"))
    except (ValueError, OverflowError, NotImplementedError):
        return _say_number(raw)


def _currency_mag(m: re.Match[str]) -> str:
    sym, amount, mag = m.group(1), m.group(2), m.group(3).lower()
    return f"{_say_number(amount)} {_MAGNITUDE[mag]} {_CURRENCY_WORDS[sym]}"


def _currency(m: re.Match[str]) -> str:
    sym, amount = m.group(1), m.group(2)
    clean = amount.replace(",", "")
    unit = _CURRENCY_SINGULAR[sym] if clean in ("1", "1.00") else _CURRENCY_WORDS[sym]
    return f"{_say_number(amount)} {unit}"


def _ordinal(m: re.Match[str]) -> str:
    try:
        return _tidy_number_words(num2words(int(m.group(1)), to="ordinal"))
    except (ValueError, OverflowError):
        return m.group(0)


def _degrees(m: re.Match[str]) -> str:
    scale = {"C": "Celsius", "F": "Fahrenheit", "K": "Kelvin"}[m.group(2)]
    return f"{_say_number(m.group(1))} degrees {scale}"


def _unit(m: re.Match[str]) -> str:
    return f"{_say_number(m.group(1))} {_UNITS[m.group(2).lower()]}"


def _acronym(m: re.Match[str]) -> str:
    word = m.group(1)
    if word in _RESPELL_ACRONYM:
        return _RESPELL_ACRONYM[word]
    if word in _KEEP_CAPS or word in _WORD_ACRONYMS:
        return word
    # A real word in capitals is emphasis, not an initialism. Reading "HUGE" as
    # "H U G E" is the single most jarring thing the old rule did.
    if word.lower() in _english_words():
        return word.lower()
    return word


def expand_text(text: str) -> str:
    """Rewrite symbols, numbers and abbreviations into spoken form.

    Order matters: URLs and currency are consumed before the bare-number rule
    can reach the digits inside them.
    """
    text = _URL.sub(lambda m: m.group(1).replace(".", " dot "), text)
    text = _EMAIL.sub(lambda m: "an email address", text)

    text = _CURRENCY_MAG.sub(_currency_mag, text)
    text = _CURRENCY.sub(_currency, text)
    text = _PERCENT.sub(lambda m: f"{_say_number(m.group(1))} percent", text)
    text = _DEGREES.sub(_degrees, text)
    text = _UNIT.sub(_unit, text)
    text = _ORDINAL.sub(_ordinal, text)

    text = _ABBREV_RE.sub(lambda m: _ABBREVIATIONS[m.group(0)], text)

    text = _YEAR_RANGE.sub(lambda m: f"{_say_year(m.group(1))} to {_say_year(m.group(2))}", text)
    text = _YEAR.sub(lambda m: _say_year(m.group(1)), text)
    text = _NUMBER.sub(lambda m: _say_number(m.group(1)), text)

    text = text.replace("&", " and ").replace("@", " at ")
    text = re.sub(r"(?<=\w)/(?=\w)", " slash ", text)
    text = _SPACES.sub(" ", text)
    return text


# --------------------------------------------------------------------------
# split
# --------------------------------------------------------------------------

# Words whose trailing period is not a sentence end. Kept lowercase, matched
# case-insensitively against the token preceding the period.
_NON_TERMINAL = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "mt", "rev", "gen", "sen",
    "rep", "gov", "vs", "etc", "inc", "ltd", "co", "corp", "dept", "est", "fig",
    "al", "approx", "eg", "ie", "no", "vol", "pp", "cf", "ca", "viz", "min",
    "max", "sec", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept",
    "oct", "nov", "dec",
}

_BOUNDARY = re.compile(r'([.!?]+["\')\]]*)(\s+|$)')
_CLAUSE = re.compile(r"(?<=[,;:])\s+|\s+-\s+|\s+(?=(?:and|but|which|because|while|although)\s)")


def _is_sentence_end(text: str, dot_index: int) -> bool:
    """False when the period belongs to an abbreviation or an initial."""
    head = text[:dot_index]
    token = re.search(r"([A-Za-z][\w'-]*)$", head)
    if not token:
        return True
    word = token.group(1)
    if word.lower() in _NON_TERMINAL:
        return False
    # A single capital letter is an initial: "J. R. R. Tolkien".
    return not (len(word) == 1 and word.isupper())


def _raw_sentences(paragraph: str) -> list[str]:
    out: list[str] = []
    start = 0
    for m in _BOUNDARY.finditer(paragraph):
        if not _is_sentence_end(paragraph, m.start(1)):
            continue
        piece = paragraph[start : m.end(1)].strip()
        if piece:
            out.append(piece)
        start = m.end()
    tail = paragraph[start:].strip()
    if tail:
        out.append(tail)
    return out


def _hard_split(sentence: str, max_chars: int) -> list[str]:
    """Break an over-long sentence at clause boundaries, never mid-word."""
    if len(sentence) <= max_chars:
        return [sentence]
    parts = [p for p in _CLAUSE.split(sentence) if p and p.strip()]
    chunks: list[str] = []
    buf = ""
    for part in parts:
        candidate = f"{buf} {part}".strip() if buf else part
        if buf and len(candidate) > max_chars:
            chunks.append(buf)
            buf = part
        else:
            buf = candidate
    if buf:
        chunks.append(buf)

    # Anything still too long has no clause boundaries: fall back to words.
    final: list[str] = []
    for chunk in chunks:
        while len(chunk) > max_chars:
            cut = chunk.rfind(" ", 0, max_chars)
            if cut <= 0:
                break
            final.append(chunk[:cut])
            chunk = chunk[cut + 1 :]
        if chunk:
            final.append(chunk)
    return final


def split_chunks(
    text: str,
    min_chars: int = MIN_CHUNK_CHARS,
    max_chars: int = MAX_CHUNK_CHARS,
    first_chars: int = FIRST_CHUNK_CHARS,
    target_chars: int = TARGET_CHUNK_CHARS,
) -> list[tuple[str, bool]]:
    """Split into synthesis chunks, flagging the ones that end a paragraph.

    Sentences are packed together up to `target_chars` so Kokoro can carry
    intonation across a full stop rather than restarting at every one. Only the
    very first chunk uses the smaller `first_chars`, because it alone decides
    how soon audio starts; by the time it is playing the prefetch is ahead.
    """
    chunks: list[tuple[str, bool]] = []
    for paragraph in text.split("\n\n"):
        paragraph = " ".join(paragraph.split())
        if not paragraph:
            continue
        sentences: list[str] = []
        for sentence in _raw_sentences(paragraph):
            sentences.extend(_hard_split(sentence, max_chars))

        packed: list[str] = []
        for sentence in sentences:
            if not packed:
                packed.append(sentence)
                continue
            # A fragment too short to carry its own prosody sounds clipped
            # whether it leads or trails, so either side being tiny forces a
            # merge even past the target - but never past the hard ceiling.
            target = first_chars if len(chunks) + len(packed) == 1 else target_chars
            tiny = len(packed[-1]) < min_chars or len(sentence) < min_chars
            combined = len(packed[-1]) + 1 + len(sentence)
            if (combined <= target or tiny) and combined <= max_chars:
                packed[-1] = f"{packed[-1]} {sentence}"
                continue
            packed.append(sentence)

        chunks.extend((c, i == len(packed) - 1) for i, c in enumerate(packed))
    return chunks


def split_sentences(
    text: str, min_chars: int = MIN_CHUNK_CHARS, max_chars: int = MAX_CHUNK_CHARS
) -> list[str]:
    """The chunk texts alone, for callers that do not care about paragraphs."""
    return [c for c, _ in split_chunks(text, min_chars, max_chars)]


def expand_acronyms(text: str) -> str:
    """Hand each initialism to the G2P in the form it reads correctly.

    Left intact, the G2P already spells "CIA" as see-eye-ay and says "NASA" as
    a word. Spacing the letters ourselves was worse than doing nothing: a lone
    capital A is the article, so "C I A" came out "see eye uh" and "A I" came
    out "uh eye". All this does now is lower-case shouted words and respell the
    handful the G2P mispronounces.
    """
    return _ACRONYM.sub(_acronym, text)


def normalize_chunks(text: str) -> list[tuple[str, bool]]:
    """clean -> disambiguate -> expand -> split -> fix acronyms; the whole pipeline.

    Each chunk is paired with whether it ends a paragraph, so the pause after it
    can be longer than the one between sentences.
    """
    chunks = split_chunks(expand_text(disambiguate_text(clean_text(text))))
    return [(expand_acronyms(c), para_end) for c, para_end in chunks]


def normalize(text: str) -> list[str]:
    """The spoken chunks alone."""
    return [c for c, _ in normalize_chunks(text)]
