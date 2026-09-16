"""The Alerts page against the design that produced it.

Chunk 5 part 2b of the fableUI rollout (dev/changelog/446) converted /alerts onto
the generic components - it has no DESIGN.md section of its own, so what it must
obey is sections 3.3, 3.4, 3.6, 3.10 and 3.14. Each case here is a decision that
a careless edit would quietly undo:

  * The severity badge is style.css's shared `.alert-severity` + `.sev-*`, the
    same one the nav alert banner and notifications_settings.html use. The page
    used to restate that rule in its own <style> block in old rgba() literals,
    which - being page-local - silently won for this page alone. A re-declaration
    is the defect returning, so it is asserted against.
  * Severity colors the row's left EDGE, never its background. DESIGN.md 3.4
    rules status tints out app-wide, and the tint is what this page drew.
  * Severity rides on a data attribute rather than a `sev-ERROR` class on the
    row, precisely because `.sev-*` IS the badge rule - putting it on the row
    would give the row the badge's background and reintroduce the tint.
  * Row actions live behind a kebab (3.6), not as loose buttons in the row.
  * Read and dismissed are different states with different actions. A row offers
    only the actions still available to it, which is the half a bulk "mark all"
    path is most likely to get wrong.

The page's JavaScript moved to static/js/alerts.js in the same change; an inline
<script> here would mean it came back.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_alerts_page_conformance
"""
import os
import re
import unittest
from datetime import datetime, timedelta

from tests.support.app import make_test_app
from app import db
from app.database import Alert

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _div_at(html, start):
    """The full <div>...</div> beginning at `start`, matched by depth.

    A row contains nested divs and the markup after the last row is more divs, so
    "up to the next row opener" and "up to the next </div>" are both wrong - each
    one silently folds the card's own closing markup into the final row and makes
    every per-row assertion pass for the wrong reason.
    """
    depth = 0
    for m in re.finditer(r'<div\b|</div>', html[start:]):
        depth += 1 if m.group(0) == '<div' else -1
        if depth == 0:
            return html[start:start + m.end()]
    raise AssertionError('unbalanced <div> in the rendered page')


def _alert(**kw):
    kw.setdefault('alert_type', 'TEST')
    kw.setdefault('severity', 'ERROR')
    kw.setdefault('title', 'Something happened')
    kw.setdefault('created_at', datetime.utcnow())
    a = Alert(**kw)
    db.session.add(a)
    return a


class AlertsPageConformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # One app and one render for the whole class: every case reads the markup and
        # none rewrites the state it was rendered from (dev/changelog/979).
        cls.t = make_test_app()
        cls.client = cls.t.app.test_client()
        with cls.t.app.app_context():
            _alert(severity='CRIT', title='Capture died', body='a long traceback\nsecond line')
            _alert(severity='WARN', title='Stream stalled', read_at=datetime.utcnow())
            _alert(severity='INFO', title='Sync finished',
                   read_at=datetime.utcnow(), dismissed_at=datetime.utcnow())
            db.session.commit()
        cls.html = cls.client.get('/alerts').get_data(as_text=True)
        # The dismissed alert is only on the page that asks for it, so the
        # "what can this row still do" cases read the include_dismissed view.
        cls.html_all = cls.client.get('/alerts?include_dismissed=1').get_data(as_text=True)

    @classmethod
    def tearDownClass(cls):
        cls.t.cleanup()

    def _rows(self, html=None):
        """Each rendered .al-row, as its own balanced chunk of markup."""
        html = self.html if html is None else html
        return [_div_at(html, m.start())
                for m in re.finditer(r'<div class="al-row[^"]*"', html)]

    def test_the_page_renders(self):
        self.assertEqual(self.client.get('/alerts').status_code, 200)

    def test_page_has_exactly_one_h1_and_it_names_the_page(self):
        """DESIGN.md 3.10 - the page used to title itself with an h2."""
        h1s = re.findall(r'<h1[^>]*>(.*?)</h1>', self.html, re.S)
        self.assertEqual(len(h1s), 1)
        self.assertIn('Alerts', h1s[0])

    def test_the_list_is_a_card(self):
        """DESIGN.md 3.3 - the rows used to sit loose on the page background. The seeded
        alerts are all one-time types, so they render in the Past card (dev/changelog/932)."""
        self.assertRegex(self.html, r'<div class="card" id="al-card-past">\s*<div class="card-head">')
        self.assertIn('<div id="alerts-past">', self.html)

    def test_severity_is_a_row_edge_not_a_row_tint(self):
        """DESIGN.md 3.4: a status renders as a badge plus optionally the row's
        left edge color - never a row tint. The page previously set a tinted
        `background` on `.alert-row.unread` for each severity. The edge is now
        a 3px inset bar (`::before`, clipped by the row's own overflow:hidden
        so it wraps the rounded corner - dev/changelog/878) rather than a
        border-left, so its color lives on the `::before` selector's own
        `background`, not on the row element itself."""
        style = re.search(r'<style>(.*?)</style>', self.html, re.S).group(1)
        edge_rules = re.findall(r'\.al-row\.unread\[data-sev="(\w+)"\]::before\s*{([^}]*)}', style)
        self.assertEqual(sorted(s for s, _ in edge_rules), ['CRIT', 'ERROR', 'INFO', 'WARN'])
        for sev, body in edge_rules:
            self.assertIn('background', body, sev)
        # The row element itself must never carry a per-severity background -
        # that is exactly the tint 3.4 forbids; only its ::before bar may.
        row_rules = re.findall(r'\.al-row\.unread\[data-sev="\w+"\](::before)?\s*{([^}]*)}', style)
        for is_before, body in row_rules:
            if not is_before:
                self.assertNotIn('background', body)

    def test_the_row_carries_severity_as_data_not_as_the_badge_class(self):
        """`.sev-*` is the shared BADGE rule (style.css). On the row it would
        paint the badge's background, which is the tint 3.4 forbids."""
        rows = self._rows()
        self.assertTrue(rows)
        for row in rows:
            opening = row.split('>')[0]
            self.assertRegex(opening, r'data-sev="(CRIT|ERROR|WARN|INFO)"')
            self.assertNotRegex(opening, r'class="[^"]*sev-')

    def test_the_severity_badge_is_the_shared_component(self):
        """style.css owns .alert-severity/.sev-*; the page must not re-declare it."""
        self.assertIn('<span class="alert-severity sev-CRIT">CRIT</span>', self.html)
        style = re.search(r'<style>(.*?)</style>', self.html, re.S).group(1)
        self.assertNotRegex(style, r'\.alert-severity\s*{')
        self.assertNotRegex(style, r'\.sev-\w+\s*{')

    def test_row_actions_are_behind_a_kebab(self):
        """DESIGN.md 3.6. Every row gets exactly one menu, and the actions are
        inside it rather than loose in the row."""
        rows = self._rows(self.html_all)
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertEqual(row.count('data-menu'), 1)
            self.assertEqual(row.count('<div class="menu pop-left">'), 1)
            for act in re.findall(r'data-act="(read|dismiss|ignore)"', row):
                idx = row.index(f'data-act="{act}"')
                self.assertLess(row.index('<div class="menu pop-left">'), idx, act)

    def test_a_row_offers_only_the_actions_still_open_to_it(self):
        """Read and dismissed are separate states. An already-read alert must not
        offer Mark read, and a dismissed one must not offer Dismiss or Ignore."""
        by_title = {re.search(r'<div class="al-title">(.*?)</div>', r, re.S).group(1): r
                    for r in self._rows(self.html_all)}
        unread = by_title['Capture died']
        self.assertIn('data-act="read"', unread)
        self.assertIn('data-act="dismiss"', unread)
        self.assertIn('data-act="ignore"', unread)

        read_only = by_title['Stream stalled']
        self.assertNotIn('data-act="read"', read_only)
        self.assertIn('data-act="dismiss"', read_only)
        self.assertIn('data-act="ignore"', read_only)

        gone = by_title['Sync finished']
        self.assertNotIn('data-act="read"', gone)
        self.assertNotIn('data-act="dismiss"', gone)
        self.assertNotIn('data-act="ignore"', gone)

    def test_unread_is_a_class_on_the_row_the_edge_rules_key_off(self):
        by_title = {re.search(r'<div class="al-title">(.*?)</div>', r, re.S).group(1): r
                    for r in self._rows()}
        self.assertIn('al-row unread', by_title['Capture died'].split('>')[0])
        self.assertNotIn('unread', by_title['Stream stalled'].split('>')[0])

    def test_the_alert_body_is_a_disclosure_and_starts_closed(self):
        """The body is a raw multi-line string. It renders hidden behind a
        toggle rather than clipped to a fixed height with no way to see the rest;
        `hidden` works because the global reset carries [hidden] (DESIGN.md 16.6)."""
        row = [r for r in self._rows() if 'Capture died' in r][0]
        self.assertIn('data-act="detail"', row)
        self.assertIn('<div class="al-detail" hidden>', row)
        # An alert with no body gets no toggle at all.
        self.assertNotIn('data-act="detail"', [r for r in self._rows()
                                               if 'Stream stalled' in r][0])

    def test_the_page_script_is_a_file_not_an_inline_block(self):
        body = self.html.split('{% block content %}')[-1]
        self.assertIn('js/alerts.js', body)
        # base.html supplies its own inline scripts; what must not come back is an
        # inline block carrying this page's handlers.
        self.assertNotIn('function markRead', body)
        self.assertNotIn('function dismissAll', body)

    def test_dismissed_alerts_are_hidden_by_default_and_reachable(self):
        self.assertNotIn('Sync finished', self.html)
        with_dismissed = self.client.get('/alerts?include_dismissed=1').get_data(as_text=True)
        self.assertIn('Sync finished', with_dismissed)
        self.assertIn('include_dismissed=1', self.html)

    def test_the_empty_state_is_the_shared_component(self):
        """DESIGN.md 3.14 - the page had its own `.alerts-empty` variant."""
        # A fresh app has no alerts; deleting the class's rows would rewrite the state the
        # shared render came from.
        empty = make_test_app()
        try:
            html = empty.client.get('/alerts').get_data(as_text=True)
        finally:
            empty.cleanup()
        self.assertIn('<div class="empty-state">', html)
        self.assertNotIn('alerts-empty', html)

    def test_no_em_dash_in_the_template_or_its_script(self):
        """CLAUDE.md: no em dashes in anything this project outputs. Asserted on
        the two files the alerts page owns rather than on the rendered page, since
        base.html still carries grandfathered ones."""
        for path in ('templates/alerts.html', 'static/js/alerts.js'):
            with open(os.path.join(REPO, path), encoding='utf-8') as fh:
                self.assertNotIn('—', fh.read(), path)


class AlertsPageStaleCountTests(unittest.TestCase):
    """The header count is the server's own number, not a hardcoded one - the
    JS re-reads it after every action, so it has to be addressable."""

    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.t.cleanup()

    def test_unread_count_is_rendered_and_addressable(self):
        with self.t.app.app_context():
            _alert(title='one')
            _alert(title='two')
            _alert(title='three', read_at=datetime.utcnow() - timedelta(minutes=1))
            db.session.commit()
        html = self.client.get('/alerts').get_data(as_text=True)
        self.assertRegex(html, r'id="al-unread"[^>]*>\s*2 unread')


class AlertsActivePastSplitTests(unittest.TestCase):
    """The page's two cards (dev/changelog/932).

    "Active alerts" holds problems that are STILL TRUE - limited to the types the app
    dismisses by itself, because the card offers no Dismiss and a row nothing could ever
    clear would be stuck in it. "Past alerts" holds everything else. The governing
    constraint, recorded at dev/changelog/932: "if something is going to be called `Active
    Alerts` or even `Still Happening` then it needs to be limited to items that will clear
    automatically."

    The no-Dismiss half is asserted on the ROUTES as well as the markup. A hidden menu item
    is presentation, and CLAUDE.md's "enforcement lives server-side" rule exists because a
    stale page, a replayed request or a bulk action walks straight around presentation.
    """

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.t.cleanup()

    def _seed(self):
        """One of each: a standing problem the app clears itself, and a one-time failure."""
        with self.t.app.app_context():
            active = _alert(alert_type='STORAGE_PATH_UNUSABLE', severity='ERROR',
                            title='DVR output directory is unusable', source='/dvr')
            past = _alert(alert_type='CONCATENATION_FAILED', severity='ERROR',
                          title='Concatenation failed', source='concatenator')
            db.session.commit()
            return active.id, past.id

    def _card(self, html, which):
        marker = f'<div class="card" id="al-card-{which}">'
        return _div_at(html, html.index(marker)) if marker in html else None

    def _titles(self, card):
        return re.findall(r'<div class="al-title">(.*?)</div>', card or '', re.S)

    def test_a_still_true_problem_is_active_and_a_one_time_failure_is_past(self):
        self._seed()
        html = self.client.get('/alerts').get_data(as_text=True)
        self.assertEqual(self._titles(self._card(html, 'active')),
                         ['DVR output directory is unusable'])
        self.assertEqual(self._titles(self._card(html, 'past')), ['Concatenation failed'])

    def test_an_active_row_offers_no_dismiss_and_says_why(self):
        self._seed()
        html = self.client.get('/alerts').get_data(as_text=True)
        card = self._card(html, 'active')
        self.assertNotIn('data-act="dismiss"', card)
        self.assertIn('Clears itself once fixed', card)
        # Mark read and Ignore are untouched: neither one claims the problem is over.
        self.assertIn('data-act="read"', card)
        self.assertIn('data-act="ignore"', card)

    def test_a_past_row_still_offers_dismiss(self):
        self._seed()
        card = self._card(self.client.get('/alerts').get_data(as_text=True), 'past')
        self.assertIn('data-act="dismiss"', card)

    def test_a_dismissed_self_clearing_alert_is_past_not_active(self):
        """Dismissed means the row is no longer describing a live condition, so it is
        history - and a card with no Dismiss is no place for a row already dismissed."""
        with self.t.app.app_context():
            now = datetime.utcnow()
            _alert(alert_type='STORAGE_PATH_UNUSABLE', severity='ERROR',
                   title='an old storage problem', source='/dvr-old',
                   read_at=now, dismissed_at=now)
            db.session.commit()
        html = self.client.get('/alerts?include_dismissed=1').get_data(as_text=True)
        self.assertIsNone(self._card(html, 'active'))
        self.assertEqual(self._titles(self._card(html, 'past')), ['an old storage problem'])

    def test_the_dismiss_route_refuses_an_active_alert(self):
        active_id, _ = self._seed()
        resp = self.client.post(f'/api/alerts/{active_id}/dismiss')
        self.assertEqual(resp.status_code, 409)
        self.assertIn('still happening', resp.get_json()['error'])
        with self.t.app.app_context():
            self.assertIsNone(db.session.get(Alert, active_id).dismissed_at)

    def test_the_dismiss_route_still_dismisses_a_past_alert(self):
        _, past_id = self._seed()
        resp = self.client.post(f'/api/alerts/{past_id}/dismiss')
        self.assertEqual(resp.status_code, 200)
        with self.t.app.app_context():
            self.assertIsNotNone(db.session.get(Alert, past_id).dismissed_at)

    def test_dismiss_all_read_leaves_a_read_active_alert_standing(self):
        """The bulk path is the easiest route to hiding every standing problem at once, so
        it is excluded in the UPDATE rather than only in the row markup."""
        with self.t.app.app_context():
            now = datetime.utcnow()
            active = _alert(alert_type='STORAGE_PATH_UNUSABLE', severity='ERROR',
                            title='DVR output directory is unusable', source='/dvr',
                            read_at=now)
            past = _alert(alert_type='CONCATENATION_FAILED', severity='ERROR',
                          title='Concatenation failed', source='concatenator', read_at=now)
            db.session.commit()
            active_id, past_id = active.id, past.id
        self.assertEqual(self.client.post('/api/alerts/dismiss_all').status_code, 200)
        with self.t.app.app_context():
            self.assertIsNone(db.session.get(Alert, active_id).dismissed_at,
                              'a problem that is still happening was dismissed in bulk')
            self.assertIsNotNone(db.session.get(Alert, past_id).dismissed_at)

    def test_the_json_list_says_which_rows_are_still_happening(self):
        self._seed()
        by_title = {a['title']: a for a in self.client.get('/api/alerts').get_json()}
        active = by_title['DVR output directory is unusable']
        past = by_title['Concatenation failed']
        self.assertTrue(active['self_clearing'])
        self.assertTrue(active['is_active_problem'])
        # Typed deliberately without a clearing path: nothing re-runs a concatenation that
        # found nothing, so it is Past by nature (dev/changelog/930).
        self.assertFalse(past['self_clearing'])
        self.assertFalse(past['is_active_problem'])

    def test_the_intro_explains_both_cards(self):
        html = self.client.get('/alerts').get_data(as_text=True)
        self.assertIn('Active alerts are problems that are still true right now', html)
        self.assertIn('so it has no Dismiss', html)
        self.assertIn('Past alerts already happened', html)

    def test_the_empty_state_still_appears_when_neither_card_has_rows(self):
        html = self.client.get('/alerts').get_data(as_text=True)
        self.assertIn('<div class="empty-state">', html)
        self.assertIsNone(self._card(html, 'active'))
        self.assertIsNone(self._card(html, 'past'))


class AlertsChannelLifecycleLinkTests(unittest.TestCase):
    """SYNC_CHANNELS_NEW / SYNC_CHANNELS_MISSING deep-link into the channel search's
    `other` filter, pre-filtered to the account they're about (dev/changelog/479). A
    synthetic alert row is enough: triggering a real provider sync is out of scope for a
    page-conformance test."""

    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.t.cleanup()

    def test_new_and_missing_alerts_link_to_their_filtered_view(self):
        with self.t.app.app_context():
            from tests.support import seed
            acct = seed.make_account(name='Gamma')
            _alert(alert_type='SYNC_CHANNELS_NEW', title='Gamma: 3 new channel(s)',
                  source=f'account:{acct.id}:channels-new')
            _alert(alert_type='SYNC_CHANNELS_MISSING', title='Gamma: 2 channel(s) missing',
                  source=f'account:{acct.id}:channels-missing')
            db.session.commit()
            acct_id = acct.id
        html = self.client.get('/alerts').get_data(as_text=True)
        self.assertIn(f'/channels?f.other=new&amp;f.acct={acct_id}', html)
        self.assertIn(f'/channels?f.other=removed&amp;f.acct={acct_id}', html)
        self.assertIn('>Channels &rarr;', html)

    def test_an_alert_with_no_recognizable_source_gets_no_link(self):
        """A malformed or missing `source` must not crash the page - it just gets no
        deep link, the same as any other alert type."""
        with self.t.app.app_context():
            _alert(alert_type='SYNC_CHANNELS_NEW', title='no source at all')
            _alert(alert_type='SYNC_CHANNELS_NEW', title='malformed source',
                  source='not-the-expected-shape')
            db.session.commit()
        html = self.client.get('/alerts').get_data(as_text=True)
        self.assertEqual(html.count('>Channels &rarr;'), 0)
