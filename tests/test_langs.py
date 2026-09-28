import pytest

from langs import lang, lang_list


@pytest.mark.parametrize(("code", "expected"), [
    ("ita", "ita"), ("IT", "ita"), (" it ", "ita"), ("eng", "eng"), ("en", "eng"),
    ("fre", "fra"), ("fra", "fra"), ("fr", "fra"),
    ("ger", "deu"), ("de", "deu"), ("dut", "nld"), ("gre", "ell"), ("chi", "zho"),
    ("jpn", "jpn"), ("xyz", "xyz"), ("", "und"),
])
def test_lang(code: str, expected: str) -> None:
    assert lang(code) == expected


def test_lang_list() -> None:
    assert lang_list("ita, ENG,fre") == ["ita", "eng", "fra"]
    assert lang_list("it,,de ,") == ["ita", "deu"]
    assert lang_list("") == []
