"""The one definition of the marks providers write into a program's title.

Providers flag live and new airings by appending superscript letters ("NASCAR Cup Series ᴸᶦᵛᵉ",
"... ᴺᵉʷ", "ᵁᴴᴰ", "ᴿᴬᵂ") or by prefixing "Live:", and the same program carries different marks
on different channels at the same moment. Anything that asks "is this the same program" has to
see through them: an auto-record rule's ledger anchor, its existing and repeat checks
(app/auto_rules.py), and the EPG source comparison (epg_sources.comparison_title()).

The definition is deliberately narrow (DESIGN-autoschedule.md §3.3). A comparison can live with
a false match; a rule cannot, because a false match skips a recording. So this strips exact
code-point ranges rather than every Unicode `Lm` character - Japanese `ー` and `々` are `Lm` and
are ordinary text - and only a "Live:" prefix with its colon, never a bare "Live ", which starts
real titles.
"""
import re

#: Measured over the 869,482 programs in the two provider samples on disk: these ranges cover
#: every superscript or modifier character that occurs in a title.
_MARK_RANGES = (
    (0x02B0, 0x02FF),   # Spacing Modifier Letters
    (0x1D00, 0x1DBF),   # Phonetic Extensions + Phonetic Extensions Supplement
    (0x2070, 0x209F),   # Superscripts and Subscripts
    (0x2C7C, 0x2C7D),   # the modifier letters ⱼ and ⱽ
)
_MARK_CHARS = frozenset({0x00B9, 0x00B2, 0x00B3})   # ¹ ² ³

_LIVE_PREFIX = re.compile(r'^\s*live:\s*', re.IGNORECASE)
_WHITESPACE = re.compile(r'\s+')


def _is_mark(ch: str) -> bool:
    cp = ord(ch)
    return cp in _MARK_CHARS or any(lo <= cp <= hi for lo, hi in _MARK_RANGES)


def strip_marks(title: str | None) -> str:
    """`title` with the providers' marks removed and its whitespace collapsed - still in its
    own case, so it is fit to show as well as to compare."""
    text = ''.join(ch for ch in (title or '') if not _is_mark(ch))
    text = _LIVE_PREFIX.sub('', text)
    return _WHITESPACE.sub(' ', text).strip()


def title_key(title: str | None) -> str:
    """A title as identity compares it: marks stripped, whitespace collapsed, casefolded.
    Nothing else is merged - "Cup Series - Coke Zero Sugar 400" and "Cup Series" with that
    episode stay different titles."""
    return strip_marks(title).casefold()
