"""normalize_handle: salvage sloppy sheet entries (strip @ and whitespace),
reject the genuinely unusable."""

from setup.sheets import normalize_handle


def test_salvages_at_and_spaces():
    assert normalize_handle("@Duong Le") == "duongle"     # the real-world case
    assert normalize_handle("  @Henry_Bai06 ") == "henry_bai06"
    assert normalize_handle("@ mona_rcx") == "mona_rcx"
    assert normalize_handle("starryweb") == "starryweb"    # already clean
    assert normalize_handle("BFanL_ok") == "bfanl_ok"      # lowercased


def test_handle_without_at_is_equivalent_to_with_at():
    # a handle typed WITHOUT '@' must be processed exactly like one WITH '@'
    assert normalize_handle("saraph11a") == normalize_handle("@saraph11a") == "saraph11a"
    assert normalize_handle("Henry_Bai06") == normalize_handle("@Henry_Bai06") == "henry_bai06"


def test_rejects_unsalvageable():
    assert normalize_handle("") is None
    assert normalize_handle(None) is None
    assert normalize_handle("ab") is None            # too short (<5)
    assert normalize_handle("123abc") is None        # must start with a letter
    assert normalize_handle("duong@le") is None      # illegal @ mid-string
    assert normalize_handle("has-a-dash") is None    # '-' not allowed
