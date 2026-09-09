"""Standing-option helpers for the channel-search tests.

Since the standing options were rephrased as "Show ..." (dev/changelog/778), the standing
SET and the standing BEHAVIOR are no longer the same thing: a `show*` key removes rows while
it is ABSENT, so `frozenset()` - which used to spell "nothing is being hidden" - is now the
most restrictive search the engine can express, and `standing=` in a URL is its most
restrictive query string.

Every test that wants "just show me what matches" therefore has to name what it wants
hidden rather than passing an empty set. It is spelled once, here, because three test
modules ask for it and a fourth will: a local copy is a copy that can be missed by the next
rename and silently start asserting the inverse of what it says.
"""
from app.channel_search import STANDING_OPTIONS


def only_hiding(*keys):
    """The standing set in which exactly `keys` remove rows, and nothing else does.

    Derived from the registry, so an option added later is accounted for without a sweep
    through the callers. An unknown key is an assertion rather than a silently-ignored
    string - that is the whole failure mode this helper exists to prevent.
    """
    unknown = set(keys) - {s.key for s in STANDING_OPTIONS}
    assert not unknown, f'unknown standing option(s): {sorted(unknown)}'
    return frozenset(s.key for s in STANDING_OPTIONS
                     if (s.key in keys) == s.hides_when_on)


def show_all_query() -> str:
    """`only_hiding()` as a URL query fragment, for the endpoint tests."""
    return '&'.join(f'standing={k}' for k in sorted(only_hiding()))


def unfolded_query(grain=None) -> str:
    """The grain's DEFAULT standing set plus `showmembers`, as a URL query fragment.

    "Everything the default search does, and keep group members' own rows." Since
    dev/changelog/860 that IS the default - the fold went the other way, because this page
    is where you go to find a channel and a search that answers "no such channel" because it
    is in a group is the hidden behavior this project refuses. So this is now a request for
    what a bare search already gives, spelled explicitly.

    Kept, and kept explicit, rather than deleted: its callers are asking about a CHANNEL's
    own payload, and what they need is that the member rows are present - which is a
    different statement from "whatever the default happens to be this month". Nothing here
    has to move if the default flips back.
    """
    from app.channel_search import GRAIN_CHANNELS, default_standing_for
    keys = set(default_standing_for(grain or GRAIN_CHANNELS)) | {'showmembers'}
    return '&'.join(f'standing={k}' for k in sorted(keys))
