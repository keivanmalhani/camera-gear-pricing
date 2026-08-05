"""Tests for title normalisation and model matching.

The dirty-title table is the point of this file. Every entry is a shape that
actually shows up in used-gear listings.
"""

from __future__ import annotations

import pytest

from gearwatch import match

SONY_REQUIRED, SONY_OPTIONAL = match.derive_tokens("Sony FE 35mm f/1.4 GM", ["gm"])
FUJI_REQUIRED, FUJI_OPTIONAL = match.derive_tokens("Fujifilm XF 56mm f/1.2 R")


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected_tokens",
    [
        ("Sony FE 35mm f/1.4 GM", {"sony", "fe", "35mm", "f1.4", "gm"}),
        ("SONY FE 35MM F1.4 GM", {"sony", "fe", "35mm", "f1.4", "gm"}),
        ("Sony FE 35 mm F/1.4 GM", {"sony", "fe", "35mm", "f1.4", "gm"}),
        ("Sony FE 35mm f 1.4 GM", {"sony", "fe", "35mm", "f1.4", "gm"}),
        ("Sony 35mm 1.4 GM", {"sony", "35mm", "f1.4", "gm"}),
        ("Sony SEL35F14GM", {"sony", "sel35f14gm", "35mm", "f1.4"}),
        ("Fujifilm XF56MMF12R", {"fujifilm", "xf", "56mm", "f1.2", "r"}),
    ],
)
def test_normalizer_collapses_spellings(raw, expected_tokens):
    produced = set(match.normalize(raw).split())
    assert expected_tokens <= produced, "missing %s" % (expected_tokens - produced)


def test_normalizer_strips_punctuation_and_seller_noise_markers():
    text = match.normalize("MINT!!  Sony   FE 35mm F1.4 GM  L@@K   *Fast*")
    assert "!" not in text and "*" not in text and "@" not in text
    assert "  " not in text
    assert "sony fe 35mm f1.4 gm" in text


def test_normalizer_is_ascii_only_and_case_insensitive():
    assert match.normalize("SONY") == match.normalize("sony")
    assert match.normalize("Objektiv Zustand") == "objektiv zustand"


def test_zoom_ranges_are_not_split_into_two_focal_lengths():
    text = match.normalize("Sony FE 24-70mm f/2.8 GM II")
    assert "24-70mm" in text.split()
    assert "70mm" not in text.split()
    assert "24mm" not in text.split()


def test_compressed_aperture_codes_expand_but_classic_stops_do_not():
    assert match._format_aperture("14") == "1.4"
    assert match._format_aperture("18") == "1.8"
    assert match._format_aperture("28") == "2.8"
    assert match._format_aperture("56") == "5.6"
    assert match._format_aperture("095") == "0.95"
    assert match._format_aperture("4") == "4"
    assert match._format_aperture("1.4") == "1.4"
    # f/11, f/16, f/22 and f/32 are real minimum-aperture markings; leave them.
    assert match._format_aperture("11") == "11"
    assert match._format_aperture("22") == "22"


def test_hyphenated_model_codes_gain_a_joined_form():
    tokens = set(match.normalize("Fujifilm X-T4 Body").split())
    assert {"x-t4", "xt4"} <= tokens


def test_content_tokens_drop_noise_but_normalize_keeps_it():
    assert "mint" in match.normalize("MINT Sony FE 35mm f/1.4 GM")
    assert "mint" not in match.content_tokens("MINT Sony FE 35mm f/1.4 GM")


def test_model_key_is_stable_across_spellings():
    assert match.model_key("Sony FE 35mm f/1.4 GM") == match.model_key(
        "sony  fe 35 MM F1.4  gm"
    )
    assert match.model_key("Sony FE 35mm f/1.4 GM") != match.model_key(
        "Sony FE 35mm f/1.8"
    )


# ---------------------------------------------------------------------------
# Token derivation
# ---------------------------------------------------------------------------


def test_derive_tokens_requires_brand_focal_and_aperture():
    assert SONY_REQUIRED == ("sony", "35mm", "f1.4", "gm")
    assert SONY_OPTIONAL == ("fe",)


def test_derive_tokens_promotes_explicit_requirements_out_of_optional():
    required, optional = match.derive_tokens("Sony FE 35mm f/1.4 GM", ["fe"])
    assert "fe" in required
    assert "fe" not in optional


def test_derive_tokens_without_an_explicit_require_leaves_gm_optional():
    required, optional = match.derive_tokens("Sony FE 35mm f/1.4 GM")
    assert required == ("sony", "35mm", "f1.4")
    assert set(optional) == {"fe", "gm"}


# ---------------------------------------------------------------------------
# The dirty-title table
# ---------------------------------------------------------------------------

DIRTY_TITLES = [
    # (title, should_match, why)
    ("Sony FE 35mm F1.4 GM SEL35F14GM Lens", True, "clean baseline"),
    ("MINT!! Sony FE 35mm F1.4 GM SEL35F14GM Lens", True, "shouty but fine"),
    ("SONY SEL35F14GM FE 35mm F/1.4 GM E-Mount Lens L@@K", True, "l@@k noise"),
    ("Sony FE 35 mm f/1.4 GM Lens - Near Mint from Japan", True, "spaced focal"),
    ("sony fe 35mm f1.4 gm g master lens", True, "all lowercase"),
    ("Sony G Master FE 35mm f/1.4 GM w/ Hood and Caps", True, "slashes"),
    ("Sony 35mm 1.4 GM lens for E mount", True, "bare aperture decimal"),
    ("Sony FE 35mm f/1.4 GM SEL35F14GM  Bundle with Filter", True, "double space"),
    # --- must NOT match ---
    ("Sony FE 35mm F1.8 SEL35F18F Wide Prime Lens", False, "near miss: f/1.8"),
    ("Sony Zeiss Distagon T* FE 35mm F1.4 ZA SEL35F14Z", False, "near miss: ZA not GM"),
    ("Sony FE 24-70mm f/2.8 GM II Lens", False, "wrong focal length"),
    ("Canon EF 35mm f/1.4L II USM Lens", False, "wrong brand"),
    ("Sony FE 35mm f/1.4 GM SEL35F14GM FOR PARTS ONLY", False, "for parts"),
    ("Sony FE 35mm f/1.4 GM lens BROKEN autofocus", False, "broken"),
    ("MINT!! Sony FE 35mm F1.4 GM SEL35F14GM Lens *READ*", False, "read"),
    ("Sony FE 35mm f/1.4 GM Lens hood only ALC-SH154", False, "hood only"),
    ("Sony FE 35mm f/1.4 GM box only no lens", False, "box only"),
    ("Sony FE 35mm f/1.4 GM sold as is, no returns", False, "as is"),
    ("Sony FE 35mm f/1.4 GM replica display dummy", False, "replica"),
    ("Sony FE 35mm f/1.4 Lens E-mount full frame", False, "missing gm"),
]


@pytest.mark.parametrize("title, expected, why", DIRTY_TITLES)
def test_dirty_title_table(title, expected, why):
    result = match.match_title(title, SONY_REQUIRED, SONY_OPTIONAL)
    assert result.matched is expected, "%s: %s -> %s" % (why, title, result.reason)


def test_non_matching_titles_explain_themselves():
    result = match.match_title(
        "Sony FE 35mm F1.8 SEL35F18F", SONY_REQUIRED, SONY_OPTIONAL
    )
    assert result.matched is False
    assert "f1.4" in result.reason
    assert result.missing_required == ("f1.4", "gm")
    assert result.score == 0.0


def test_negative_hits_are_reported_by_name():
    result = match.match_title(
        "Sony FE 35mm f/1.4 GM FOR PARTS not working", SONY_REQUIRED, SONY_OPTIONAL
    )
    assert result.matched is False
    assert "for parts" in result.negative_hits
    assert "not working" in result.negative_hits


def test_scoring_rewards_optional_token_hits():
    full = match.match_title(
        "Sony FE 35mm f/1.4 GM Lens", SONY_REQUIRED, SONY_OPTIONAL
    )
    partial = match.match_title(
        "Sony 35mm f/1.4 GM Lens", SONY_REQUIRED, SONY_OPTIONAL
    )
    assert full.score == 100.0
    assert partial.score == 70.0
    assert full.score > partial.score
    assert full.matched_optional == ("fe",)


def test_a_watch_with_no_optional_tokens_still_scores_100():
    result = match.match_title("Sony 35mm f/1.4 GM", SONY_REQUIRED, ())
    assert result.matched is True
    assert result.score == 100.0


def test_extra_exclusions_are_honoured():
    negatives = tuple(match.DEFAULT_NEGATIVE_TOKENS) + ("body only",)
    kit = match.match_title(
        "Fujifilm XF 56mm f/1.2 R body only", FUJI_REQUIRED, FUJI_OPTIONAL, negatives
    )
    assert kit.matched is False
    assert "body only" in kit.negative_hits
    # ...and is not excluded without the extra token
    assert match.match_title(
        "Fujifilm XF 56mm f/1.2 R body only", FUJI_REQUIRED, FUJI_OPTIONAL
    ).matched is True


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Fujifilm XF 56mm F1.2 R Fujinon Lens", True),
        ("Fujifilm XF56MMF12R Lens for X-Mount", True),
        ("FUJIFILM FUJINON XF 56mm f/1.2 R Portrait Lens", True),
        ("Fujifilm XF 56mm f/1.2 R APD Lens", True),
        ("Fujifilm XF 50mm f/2 R WR Lens", False),
        ("Fujifilm XF 56mm f/1.4 R Lens", False),
        ("Sony FE 55mm f/1.8 ZA", False),
    ],
)
def test_fuji_titles(title, expected):
    assert match.match_title(title, FUJI_REQUIRED, FUJI_OPTIONAL).matched is expected


def test_empty_and_garbage_titles_do_not_crash():
    assert match.normalize("") == ""
    assert match.normalize(None) == ""
    assert match.match_title("", SONY_REQUIRED).matched is False
    assert match.match_title("!!!***", SONY_REQUIRED).matched is False
