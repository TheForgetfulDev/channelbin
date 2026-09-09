"""Shared parser for the profile create/edit JSON bodies (Recording Profiles and Health
Check Profiles), behind the modal in static/js/profile-modal.js.

Both profile types are the same form wearing different labels: a name, plus a list of
typed overrides where "unset" almost always means "inherit the global config value". They
differ only in their field lists, so the parse lives here once and each route supplies a
spec. `profile-modal.js` is the client-side mirror of exactly these rules - the modal is
presentation, this is the enforcement point (CLAUDE.md), and the two must agree about what
a blank field means.

The distinction the whole module exists to protect: **blank is not zero.**

- A blank numeric override stores NULL and means "fall back to the global default".
- `0` is a value the user typed and means zero.

That gap is widest on `RecordingProfile.retention_days`, where None = use the global
retention window and 0 = never auto-delete *even if a global window is set* - i.e. the two
readings are opposites. It is equally live on `screenshots_enabled`, where False is a
profile value and None is inheritance. Any parse that leans on truthiness (`if not raw`,
`raw or default`, `Number(x) || null`) collapses them and silently rewrites what a profile
does, with nothing in the UI to show for it.

A field whose column is NOT NULL declares `blank_value` instead - the two padding columns
default to 0 - so a blank there stores that value rather than NULL.

`nullable_overrides()` at the bottom is the read side of the same distinction: both list
pages render what a profile SET and leave what it inherits off the row entirely, so the
"is this set" test has to be `is not None` there too.
"""
from typing import NamedTuple

TEXT = 'text'
INT = 'int'
BOOL = 'bool'


class ProfileField(NamedTuple):
    """One editable field.

    key         - model attribute and JSON key
    label       - what a validation error calls it; matches the modal's on-screen label
                  so the message reads the same on both sides
    kind        - TEXT | INT | BOOL (BOOL is tri-state: True / False / None)
    required    - TEXT only; an empty value is rejected rather than stored
    blank_value - what an empty or absent value stores. None (inherit) for every
                  nullable column; 0 for the NOT NULL padding columns.
    """
    key: str
    label: str
    kind: str = INT
    required: bool = False
    blank_value: object = None


def _blank(raw):
    """True for the values a cleared control can arrive as. Deliberately identity/equality
    against '' and None only - never truthiness, or 0 and False would count as blank."""
    if isinstance(raw, str):
        raw = raw.strip()
    return raw is None or raw == ''


def parse_profile_body(fields, data):
    """(values, error). `values` is keyed by field and ready to assign onto the model;
    `error` is the first failure's message, in which case values is None."""
    values = {}
    for field in fields:
        raw = data.get(field.key)

        if field.kind == TEXT:
            text = '' if raw is None else str(raw).strip()
            if not text:
                if field.required:
                    return None, f'{field.label} is required.'
                values[field.key] = field.blank_value
            else:
                values[field.key] = text
            continue

        if field.kind == BOOL:
            if _blank(raw):
                values[field.key] = field.blank_value
            elif isinstance(raw, bool):
                values[field.key] = raw
            elif isinstance(raw, str) and raw.strip() in ('true', 'false'):
                values[field.key] = raw.strip() == 'true'
            else:
                return None, f'{field.label} must be on, off, or left unset.'
            continue

        # INT
        if _blank(raw):
            values[field.key] = field.blank_value
            continue
        number_error = f'{field.label} must be a non-negative whole number.'
        # bool is an int subclass, so a stray True must not land as 1. Today the
        # int(str(raw)) below would also reject it (int('True') raises), which is belt and
        # braces - but only while the parse goes through str(). Anyone simplifying that to
        # int(raw) needs this branch still standing, so do not drop both.
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            return None, number_error
        try:
            number = int(str(raw).strip())
        except ValueError:
            return None, number_error
        if number < 0:
            return None, number_error
        values[field.key] = number

    return values, None


def profile_payload(profile, fields):
    """What the modal prefills an Edit from. Every field carries its true stored value,
    including None - the modal distinguishes "unset" from "zero" and an '' stand-in would
    collapse exactly the distinction this module protects."""
    payload = {'id': profile.id}
    for field in fields:
        payload[field.key] = getattr(profile, field.key)
    return payload


class Override(NamedTuple):
    """One "this profile changed X to Y" pair, as both list pages render it."""
    label: str
    value: str


def nullable_overrides(profile, rows):
    """[Override] for the fields this profile actually SET, in `rows` order.

    Each row is `(key, label, render)` over a NULLABLE override column, where None means
    "inherit the global value". Inclusion is `is not None` and never truthiness: a stored
    0 or False is a value the user chose, and dropping it would tell them the profile
    inherits a setting it in fact overrides - the same collapse this module exists to
    prevent on the write side. `render` sees only set values and never has to handle None.

    A NOT NULL column (the two padding fields) inherits nothing and so has no "unset"
    state to test for; those are appended by the caller, not listed here.
    """
    out = []
    for key, label, render in rows:
        value = getattr(profile, key)
        if value is not None:
            out.append(Override(label, render(value)))
    return out
