"""Tests for naming.py: folder-name sanitisation, the 80-char cap, and dedup."""

from __future__ import annotations

import hashlib

from collections2mo2 import naming


def test_sanitize_strips_illegal_characters():
    assert naming.sanitize_folder_name('A<B>C:D"E/F\\G|H?I*J') == "ABCDEFGHIJ"


def test_sanitize_collapses_whitespace():
    assert naming.sanitize_folder_name("  My   Mod   Name  ") == "My Mod Name"


def test_sanitize_strips_trailing_dots_and_spaces():
    assert naming.sanitize_folder_name("Trailing Dots...   ") == "Trailing Dots"


def test_sanitize_reserved_device_names_get_suffixed():
    assert naming.sanitize_folder_name("CON") == "CON_mod"
    assert naming.sanitize_folder_name("con") == "con_mod"
    assert naming.sanitize_folder_name("LPT1") == "LPT1_mod"


def test_sanitize_empty_or_all_illegal_never_returns_empty():
    assert naming.sanitize_folder_name("") == "unnamed_mod"
    assert naming.sanitize_folder_name("///") == "unnamed_mod"


def test_sanitize_long_name_is_capped_with_deterministic_hash_suffix():
    long_name = "A" * 100
    result = naming.sanitize_folder_name(long_name)
    assert len(result) == naming.MAX_FOLDER_NAME
    digest = hashlib.sha1(long_name.encode("utf-8")).hexdigest()[:6]
    assert result == "A" * 72 + " ~" + digest
    # deterministic across calls
    assert naming.sanitize_folder_name(long_name) == result


def test_sanitize_long_name_with_dots_at_truncation_point_is_trimmed():
    # A run of dots straddles the char-72 truncation cut, so the truncated head
    # needs its own rstrip (the name overall does not end in dots, so the first
    # rstrip in sanitize_folder_name never touches them).
    long_name = "A" * 70 + "." * 5 + "B" * 30
    result = naming.sanitize_folder_name(long_name)
    digest = hashlib.sha1(long_name.encode("utf-8")).hexdigest()[:6]
    assert result == "A" * 70 + " ~" + digest
    assert len(result) <= naming.MAX_FOLDER_NAME
    assert not result.split(" ~")[0].endswith(".")


def test_mod_folder_name_uses_source_name():
    assert naming.mod_folder_name({"name": "My Cool Mod"}) == "My Cool Mod"
    assert naming.mod_folder_name({}) == "unnamed_mod"


def test_assign_folder_names_dedup_with_tag_suffix():
    mods = [
        {"name": "Duplicate Mod", "source": {"tag": "tag1"}},
        {"name": "Duplicate Mod", "source": {"tag": "tag2"}},
        {"name": "Unique Mod", "source": {"tag": "tag3"}},
    ]
    result = naming.assign_folder_names(mods)
    assert result == {
        "tag1": "Duplicate Mod",
        "tag2": "Duplicate Mod ~tag2",
        "tag3": "Unique Mod",
    }
    # every folder name unique
    assert len(set(result.values())) == len(result)


def test_assign_folder_names_three_way_duplicate_stays_unique():
    mods = [
        {"name": "Trip Mod", "source": {"tag": "aaa"}},
        {"name": "Trip Mod", "source": {"tag": "bbb"}},
        {"name": "Trip Mod", "source": {"tag": "ccc"}},
    ]
    result = naming.assign_folder_names(mods)
    assert result == {
        "aaa": "Trip Mod",
        "bbb": "Trip Mod ~bbb",
        "ccc": "Trip Mod ~ccc",
    }
    assert len(set(result.values())) == 3


def test_separator_name():
    assert naming.separator_name(1) == "Phase 1_separator"
    assert naming.separator_name(2) == "Phase 2_separator"
    assert naming.separator_name(666) == "Optional_separator"
