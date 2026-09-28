"""
langs.py — one spelling per language: ISO 639-2 three-letter codes, "T" form.

Discs and users write the same language in different ways: DVDs use two
letters ("it"), Blu-rays usually the "bibliographic" three-letter form ("fre",
"ger"), most lists the "terminology" one ("fra", "deu"). Everything goes
through lang() before being compared or shown, so --audio-lang fra finds the
French track of a disc that says "fre".
"""

# ISO 639-1 (two letters, DVD) -> ISO 639-2
TWO_TO_THREE = {
    "en": "eng", "it": "ita", "fr": "fra", "de": "deu", "es": "spa", "pt": "por",
    "nl": "nld", "ru": "rus", "ja": "jpn", "zh": "zho", "ko": "kor", "pl": "pol",
    "cs": "ces", "hu": "hun", "sv": "swe", "da": "dan", "fi": "fin", "no": "nor",
    "el": "ell", "tr": "tur", "he": "heb", "ar": "ara", "hi": "hin", "hr": "hrv",
    "sr": "srp", "sl": "slv", "sk": "slk", "uk": "ukr", "ro": "ron", "bg": "bul",
    "ca": "cat", "et": "est", "lv": "lav", "lt": "lit", "is": "isl", "th": "tha",
    "id": "ind", "ms": "msa", "vi": "vie", "fa": "fas", "sq": "sqi", "mk": "mkd",
    "hy": "hye", "ka": "kat", "eu": "eus", "gl": "glg", "cy": "cym", "ga": "gle",
}

# the twenty languages with a different "B" (bibliographic) code
B_TO_T = {
    "alb": "sqi", "arm": "hye", "baq": "eus", "bur": "mya", "chi": "zho",
    "cze": "ces", "dut": "nld", "fre": "fra", "geo": "kat", "ger": "deu",
    "gre": "ell", "ice": "isl", "mac": "mkd", "mao": "mri", "may": "msa",
    "per": "fas", "rum": "ron", "slo": "slk", "tib": "bod", "wel": "cym",
}


def lang(code: str) -> str:
    """The canonical code: 'IT', 'it', 'ita' -> 'ita'; 'fre', 'fr' -> 'fra'."""
    c = (code or "").strip().lower()
    c = TWO_TO_THREE.get(c, c)
    return B_TO_T.get(c, c) or "und"


def lang_list(text: str) -> list[str]:
    """'ita, ENG,fre' -> ['ita', 'eng', 'fra']."""
    return [lang(x) for x in text.split(",") if x.strip()]
