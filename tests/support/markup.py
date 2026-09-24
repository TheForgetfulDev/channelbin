"""Read one value out of a rendered page instead of searching the whole document for it.

Every page carries a per-response CSRF token (about 90 random base64 characters) and inline
SVG path data, so a short needle searched across the whole HTML can be matched by either one:
a negative assertion then fails at random, and a positive one passes while the value it is
about is missing (dev/changelog/1097, dev/docs/BUGS.md 2026-09-22 @ 07:35:26 PM).
`tests/test_static_invariants.py::ShortNeedleOverWholePageTests` fails the shape.
"""
import re


def text_of(fragment):
    """The visible text of an HTML fragment: tags dropped, whitespace collapsed."""
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', fragment)).strip()


def stat_values(html, label):
    """The text of every `stat_row()` value rendered under `label`, in page order.

    A list rather than one value so the caller can assert the page renders the stat exactly
    once - a page that shows two different answers for one stat is a defect of its own.
    """
    return [text_of(m) for m in re.findall(
        r'<span class="sk">' + re.escape(label) + r'</span><span class="sv[^>]*>(.*?)</span></div>',
        html, re.S)]
