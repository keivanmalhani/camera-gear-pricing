"""Title normalisation and model matching.

Marketplace titles are filthy. A single lens shows up as all of these:

    MINT!! Sony FE 35mm F1.4 GM SEL35F14GM Lens *READ*
    Sony FE 35 mm f/1.4 G Master  [EXC+5] from Japan
    SONY SEL35F14GM FE 35mm F/1.4 GM E-Mount L@@K

They must all reduce to the same token set, and none of these may:

    Sony FE 35mm F1.8 SEL35F18F
    Sony Zeiss Distagon T* FE 35mm F1.4 ZA
    Sony FE 35mm f/1.4 GM lens hood ALC-SH154 only

The normaliser is deliberately explicit rather than clever. Every rule below is
one the tests pin down, because a matcher you cannot explain is a matcher you
cannot trust with money.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List, Sequence, Set, Tuple

__all__ = [
    "normalize",
    "tokens",
    "content_tokens",
    "model_key",
    "derive_tokens",
    "match_title",
    "MatchResult",
    "NOISE_WORDS",
    "DEFAULT_NEGATIVE_TOKENS",
    "REQUIRED_WEIGHT",
    "OPTIONAL_WEIGHT",
]

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: Pure seller fluff. Removed only when scoring optional-token overlap, never
#: before negative-token detection (otherwise "*READ*" would become invisible).
NOISE_WORDS = frozenset(
    {
        "a", "and", "beautiful", "best", "bundle", "buy", "clean", "cond",
        "condition", "deal", "excellent", "exc", "excellent+", "fast", "flawless",
        "free", "from", "gorgeous", "great", "hot", "in", "l", "look", "lqqk",
        "mint", "must", "near", "new", "nice", "nm", "of", "perfect", "pristine",
        "rare", "read", "sale", "see", "seller", "ship", "shipped", "shipping",
        "ships", "super", "superb", "the", "to", "top", "tested", "united",
        "usa", "us", "very", "w", "with", "works", "working", "worldwide", "wow",
    }
)

#: Applied to every watch unless the caller replaces them. These are the states
#: in which a price is not a comparable at all.
DEFAULT_NEGATIVE_TOKENS: Tuple[str, ...] = (
    "for parts",
    "parts only",
    "not working",
    "as is",
    "as-is",
    "broken",
    "damaged",
    "cracked",
    "fungus",
    "read",
    "repair",
    "junk",
    "empty box",
    "box only",
    "cap only",
    "hood only",
    "replica",
    "fake",
    "copy",
)

REQUIRED_WEIGHT = 0.7
OPTIONAL_WEIGHT = 0.3

# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------

_LQQK = re.compile(r"l@+k")
_APERTURE_SLASH = re.compile(r"\bf\s*/\s*(\d+(?:\.\d+)?)")
_APERTURE_SPACED = re.compile(r"\bf\s+(\d+\.\d+)\b")
_PUNCTUATION = re.compile(r"[^a-z0-9.\-]+")
_ZOOM = re.compile(r"(\d+)\s*-\s*(\d+)\s*mm")
_FOCAL = re.compile(r"(\d+(?:\.\d+)?)\s*mm")
_APERTURE_PLAIN = re.compile(r"\bf/?(\d+(?:\.\d+)?)")
#: Manufacturer part codes such as SEL35F14GM, SEL2470GM2, XF56MMF12R.
_MODEL_CODE = re.compile(r"^([a-z]{2,5})(\d{2,3})f(\d{1,3})([a-z0-9]*)$")
_ZOOM_TOKEN = re.compile(r"^\d+-\d+mm$")
_BARE_DECIMAL = re.compile(r"^\d+\.\d+$")

#: Two digit aperture codes that are far more likely to be a real minimum
#: aperture marking than a compressed maximum aperture, so they are left alone.
_LITERAL_STOPS = frozenset({"11", "16", "22", "32"})


def _format_number(value: float) -> str:
    text = ("%.3f" % value).rstrip("0").rstrip(".")
    return text or "0"


def _format_aperture(raw: str) -> str:
    """Canonicalise an aperture capture group.

    Rules, in order:

    * contains a dot  -> use as written              ("1.4" -> f1.4)
    * one digit       -> use as written              ("4"   -> f4)
    * two digits and not a classic minimum-aperture marking -> insert a decimal
      point after the first digit ("14" -> f1.4, "28" -> f2.8, "56" -> f5.6)
    * three digits starting with zero -> "0.dd"      ("095" -> f0.95)
    * anything else   -> use as written
    """
    if "." in raw:
        return _format_number(float(raw))
    if len(raw) == 1:
        return raw
    if len(raw) == 2 and raw not in _LITERAL_STOPS:
        return _format_number(float("%s.%s" % (raw[0], raw[1])))
    if len(raw) == 3 and raw.startswith("0"):
        return _format_number(float("0.%s" % raw[1:]))
    return _format_number(float(raw))


def _expand_token(token: str) -> List[str]:
    """Return ``token`` plus any derived tokens it implies."""
    out = [token]
    code = _MODEL_CODE.match(token)
    if code:
        out.append("%smm" % code.group(2))
        out.append("f%s" % _format_aperture(code.group(3)))
        return out
    if "-" in token and not _ZOOM_TOKEN.match(token):
        joined = token.replace("-", "")
        if joined and joined != token:
            out.append(joined)
        out.extend(part for part in token.split("-") if part)
        return out
    if _BARE_DECIMAL.match(token):
        value = float(token)
        # A bare decimal in a camera-gear title is an aperture often enough that
        # normalising it is worth the small risk. Bounded so prices and
        # megapixel counts are left alone.
        if 0.7 <= value <= 32.0:
            out.append("f%s" % _format_number(value))
    return out


def normalize(text: str, expand: bool = True) -> str:
    """Lowercase, de-noise punctuation, and canonicalise focal lengths/apertures.

    With ``expand=True`` (the default) derived tokens are appended inline right
    after the token that produced them, so phrase matching still works.
    """
    raw = text or ""
    lowered = raw.encode("ascii", "ignore").decode("ascii").lower()
    lowered = _LQQK.sub(" ", lowered)
    lowered = lowered.replace("@", " ")

    # Apertures written with a slash or a space must be joined before the
    # punctuation pass eats the slash.
    lowered = _APERTURE_SLASH.sub(lambda m: " f%s " % _format_aperture(m.group(1)), lowered)
    lowered = _APERTURE_SPACED.sub(lambda m: " f%s " % _format_aperture(m.group(1)), lowered)

    lowered = _PUNCTUATION.sub(" ", lowered)

    # Zoom ranges first, marked with an uppercase MM sentinel so the single
    # focal-length rule below cannot chop "24-70mm" into "24- 70mm".
    lowered = _ZOOM.sub(lambda m: " %s-%sMM " % (m.group(1), m.group(2)), lowered)
    lowered = _FOCAL.sub(lambda m: " %smm " % _format_number(float(m.group(1))), lowered)
    lowered = lowered.replace("MM", "mm")

    lowered = _APERTURE_PLAIN.sub(lambda m: " f%s " % _format_aperture(m.group(1)), lowered)

    out: List[str] = []
    for chunk in lowered.split():
        token = chunk.strip(".-")
        if not token:
            continue
        if expand:
            # Derived tokens are appended immediately after their source token,
            # never de-duplicated, so multi-word phrase matching still sees the
            # original word order.
            out.extend(_expand_token(token))
        else:
            out.append(token)
    return " ".join(out)


def tokens(text: str) -> Tuple[str, ...]:
    """All normalised tokens, noise included."""
    return tuple(normalize(text).split())


def content_tokens(text: str) -> Tuple[str, ...]:
    """Normalised tokens with seller noise removed. Used for scoring only."""
    return tuple(t for t in normalize(text).split() if t not in NOISE_WORDS)


def model_key(query: str) -> str:
    """A stable identity for a model, derived from the watch query.

    Two watches for the same lens written differently produce the same key, so
    price history accumulates per model rather than per spelling.
    """
    return " ".join(sorted(set(content_tokens(query))))


_FOCAL_TOKEN = re.compile(r"^\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?mm$")
_APERTURE_TOKEN = re.compile(r"^f\d")
_ALNUM_CODE = re.compile(r"^(?=.*[a-z])(?=.*\d)[a-z0-9]+$")


def _is_focal(token: str) -> bool:
    return bool(_FOCAL_TOKEN.match(token))


def _is_aperture(token: str) -> bool:
    return bool(_APERTURE_TOKEN.match(token))


def derive_tokens(
    query: str, extra_required: Iterable[str] = ()
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """Split a watch query into (required, optional) tokens.

    Required by default: the brand (first token), every focal length, and every
    aperture. Those three identify a lens; the rest (``fe``, ``gm``, ``ii``) are
    optional signals that raise the score. ``--require`` promotes any token.
    """
    query_tokens = [t for t in normalize(query, expand=False).split()]
    required: List[str] = []
    optional: List[str] = []
    for index, token in enumerate(query_tokens):
        if token in required or token in optional:
            continue
        if index == 0 or _is_focal(token) or _is_aperture(token):
            required.append(token)
        else:
            optional.append(token)
    for extra in extra_required:
        for token in normalize(extra, expand=False).split():
            if token in optional:
                optional.remove(token)
            if token not in required:
                required.append(token)
    return tuple(required), tuple(optional)


@dataclass(frozen=True)
class MatchResult:
    matched: bool
    score: float
    reason: str
    matched_required: Tuple[str, ...] = ()
    missing_required: Tuple[str, ...] = ()
    matched_optional: Tuple[str, ...] = ()
    negative_hits: Tuple[str, ...] = ()


def _forms(token: str) -> Tuple[str, ...]:
    """Equivalent spellings of a single token (``x-t4`` and ``xt4``)."""
    if "-" in token and not _ZOOM_TOKEN.match(token):
        return (token, token.replace("-", ""))
    return (token,)


def _present(needle: str, haystack_text: str, haystack_tokens: Set[str]) -> bool:
    parts = needle.split()
    if not parts:
        return False
    if len(parts) == 1:
        return any(form in haystack_tokens for form in _forms(parts[0]))
    return (" %s " % needle) in (" %s " % haystack_text)


def match_title(
    title: str,
    required: Sequence[str],
    optional: Sequence[str] = (),
    negative: Sequence[str] = DEFAULT_NEGATIVE_TOKENS,
) -> MatchResult:
    """Score ``title`` against a watch's token sets.

    Order matters: negative tokens are checked first and are absolute. A listing
    that says "for parts" is not a cheap copy of the lens, it is a different
    product, and letting it into the sold distribution would drag the whole band
    down.
    """
    text = normalize(title)
    token_set = set(text.split())

    negative_hits = tuple(
        item for item in negative if _present(normalize(item), text, token_set)
    )
    if negative_hits:
        return MatchResult(
            matched=False,
            score=0.0,
            reason="negative token(s): %s" % ", ".join(negative_hits),
            negative_hits=negative_hits,
        )

    required_norm = [normalize(item) for item in required]
    missing = tuple(
        original
        for original, needle in zip(required, required_norm)
        if not _present(needle, text, token_set)
    )
    if missing:
        return MatchResult(
            matched=False,
            score=0.0,
            reason="missing required token(s): %s" % ", ".join(missing),
            matched_required=tuple(t for t in required if t not in missing),
            missing_required=missing,
        )

    optional_hits = tuple(
        item for item in optional if _present(normalize(item), text, token_set)
    )
    ratio = (len(optional_hits) / len(optional)) if optional else 1.0
    score = round(100.0 * (REQUIRED_WEIGHT + OPTIONAL_WEIGHT * ratio), 1)
    return MatchResult(
        matched=True,
        score=score,
        reason="ok",
        matched_required=tuple(required),
        matched_optional=optional_hits,
    )
