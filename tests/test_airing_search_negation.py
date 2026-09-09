"""Tier 2 regression: negated search terms (`-word`) on the airing grain.

Ported from tests/test_guide_search_negation.py when `/api/guide/search` was retired with
the Extended Search modal (dev/changelog/416). The question that route answered - "when is
this on, and where" - is answered by `/api/channels/search?grain=airings` now, so the guard
moved with it rather than being deleted along with its endpoint.

The single genuine correctness risk is the SQL NULL trap, and it was live on this grain when
the guard arrived: `NOT (col LIKE p)` evaluates to NULL, not TRUE, when `col` is NULL, so an
excluded term silently DROPPED every showing with a NULL sub_title or description. Fixed in
`app/channel_search.py::_column_like` by forcing a miss on a NULL column to FALSE; see
BUGS.md 2026-07-31 08:44 PM. `test_negation_keeps_null_field_rows` is that regression, and
it is the reason this file switches the two nullable EPG fields on - the default field set
(`name` + `epg-title`) hides the defect, because `epg_entries.title` is NOT NULL.

Runs against a throwaway temp SQLite DB (make_test_app + seed) - never the live dvr.db.

    python3 -m unittest tests.test_airing_search_negation
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.channel_search import Term, parse_terms  # noqa: E402

#: Every EPG field switched on, which is what the old route always searched. `facets=`
#: asks for rows only - the facet counts are a different question and cost 10x here.
_ALL_EPG = 'grain=airings&facets=&in=epg-title&in=epg-sub&in=epg-desc'


def _titles(resp):
    """Sorted list of row titles from a /api/channels/search JSON response."""
    return sorted(r['title'] for r in resp.get_json()['rows'])


class ParseTermsTests(unittest.TestCase):
    """The engine's own parser, which replaced `routes/guide.py::_parse_search_terms`.
    Same three rules the old one had: a leading '-' on a non-empty body excludes, a bare
    '-' is dropped, and a query with no '-' is exactly its words."""

    def test_no_negation_is_identical_to_split(self):
        for q in ('supercars', 'nascar sonoma raceway', 'a b c'):
            self.assertEqual(parse_terms(q), tuple(Term(w) for w in q.split()))

    def test_leading_hyphen_marks_negative(self):
        self.assertEqual(parse_terms('supercars -highlights'),
                         (Term('supercars'), Term('highlights', exclude=True)))

    def test_multiple_negatives(self):
        self.assertEqual(
            parse_terms('nascar -highlights -preview'),
            (Term('nascar'), Term('highlights', exclude=True), Term('preview', exclude=True)))

    def test_a_bare_hyphen_is_literal_text_here(self):
        """A DELIBERATE difference from the old parser, which dropped a lone '-'. This
        engine treats anything that is not `-<body>` as literal text to find, because the
        user is describing a substring rather than writing a boolean expression - so
        `nascar - preview` looks for a hyphen too, and matches less rather than more."""
        self.assertEqual(parse_terms('nascar - preview'),
                         (Term('nascar'), Term('-'), Term('preview')))

    def test_all_negative(self):
        self.assertEqual(parse_terms('-highlights'), (Term('highlights', exclude=True),))


class AiringSearchNegationTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.acc = seed.make_account()
        self.ch = seed.make_channel(self.acc, name='Race Channel', in_guide=True)

    def tearDown(self):
        self.t.cleanup()

    def _search(self, q):
        return self.t.client.get(
            f'/api/channels/search?{_ALL_EPG}&q={q.replace(" ", "+")}')

    def test_no_negation_matches_baseline(self):
        """A query with no '-' returns exactly the rows the positive filter alone would."""
        seed.make_epg_entry(self.ch, title='Supercars Sonoma', offset_minutes=10)
        seed.make_epg_entry(self.ch, title='Supercars Highlights', offset_minutes=70)
        seed.make_epg_entry(self.ch, title='Cooking Show', offset_minutes=130)
        db.session.commit()

        resp = self._search('supercars')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_titles(resp), ['Supercars Highlights', 'Supercars Sonoma'])

    def test_basic_exclusion(self):
        """`supercars -highlights` drops the highlights row, keeps the real event."""
        seed.make_epg_entry(self.ch, title='Supercars Sonoma', offset_minutes=10)
        seed.make_epg_entry(self.ch, title='Supercars Highlights', offset_minutes=70)
        db.session.commit()

        resp = self._search('supercars -highlights')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_titles(resp), ['Supercars Sonoma'])

    def test_negation_keeps_null_field_rows(self):
        """THE key regression, and it was live on this grain: a row whose sub_title AND
        description are NULL and whose title lacks the negated word must still be returned.
        `NOT (col LIKE p)` is NULL for a NULL column, and SQLite drops a row a WHERE clause
        answers NULL for - so the showing disappears with no error anywhere."""
        # make_epg_entry leaves sub_title and description NULL by default.
        seed.make_epg_entry(self.ch, title='Supercars Sonoma', offset_minutes=10)
        db.session.commit()

        resp = self._search('supercars -highlights')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_titles(resp), ['Supercars Sonoma'])

    def test_a_null_field_still_matches_on_a_populated_sibling(self):
        """The other direction of the same fix: forcing a NULL column's LIKE to FALSE must
        not stop a row matching on the field that IS populated. A fix that answered NULL
        columns wrongly in the include direction would empty the search instead."""
        seed.make_epg_entry(self.ch, title='Race Day', description='Supercars at Sonoma',
                            offset_minutes=10)
        db.session.commit()

        resp = self._search('supercars')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_titles(resp), ['Race Day'])

    def test_multiple_negatives(self):
        """`nascar -highlights -preview` excludes rows matching either negated word."""
        seed.make_epg_entry(self.ch, title='Nascar Cup Race', offset_minutes=10)
        seed.make_epg_entry(self.ch, title='Nascar Highlights', offset_minutes=70)
        seed.make_epg_entry(self.ch, title='Nascar Preview', offset_minutes=130)
        db.session.commit()

        resp = self._search('nascar -highlights -preview')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_titles(resp), ['Nascar Cup Race'])

    def test_all_negative_query_allowed(self):
        """An all-negative query (`-highlights`) returns everything except the excluded
        word. Same decision the old route took, and the engine's planner names it as one of
        the five things that force the unindexed path - there is nothing to narrow TO."""
        seed.make_epg_entry(self.ch, title='Supercars Sonoma', offset_minutes=10)
        seed.make_epg_entry(self.ch, title='Supercars Highlights', offset_minutes=70)
        db.session.commit()

        resp = self._search('-highlights')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_titles(resp), ['Supercars Sonoma'])

    def test_a_query_of_only_bare_hyphens_is_not_an_error(self):
        """The old route 400ed here, because it required at least one parsed term. This
        engine has no minimum query length and no unparseable query: `- -` is two literal
        hyphens to look for, so it is an ordinary search that happens to match nothing.
        A behavior change, deliberate, and recorded rather than assumed - the important
        half is that it is a 200 with an honest empty result and not a 400 or a 500."""
        seed.make_epg_entry(self.ch, title='Supercars Sonoma', offset_minutes=10)
        db.session.commit()

        resp = self.t.client.get(f'/api/channels/search?{_ALL_EPG}&q=-+-')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_titles(resp), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
