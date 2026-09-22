"""Groups unification 4/4 - unified Groups UI (changelog/238, DESIGN.md §14):

  * Clone copies membership into a new group (optionally narrowed via
    `channel_ids`), always starting out of the TV Guide, and never warns about a
    format mix - a clone records from nobody (dev/changelog/1077).
  * Every group carries exactly one check, so the old attach shape
    (`attach_group_id`) is refused rather than minting a second one.
  * The Groups list page renders one section holding every group, the pinned system
    group included, and a group covered only by the automatic TV Guide check says
    so on its inherited chip.
  * The unified detail page's file-level comment describes the two flags the page is
    gated on in terms the model still has (`dev/changelog/747`).

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_groups_unified_ui
"""
import os
import re
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import template_rendered  # noqa: E402

from tests.support import make_test_app  # noqa: E402
from tests.support.seed import make_account, make_channel, make_group  # noqa: E402
from app import db  # noqa: E402
from app.database import (  # noqa: E402
    ChannelGroup, ChannelTest, OnDemandTestJob,
)


def _test(channel, resolution, fps):
    t = ChannelTest(channel_id=channel.id, test_started_at=datetime.utcnow(),
                    status='COMPLETED', resolution=resolution, fps=fps)
    db.session.add(t)
    return t


class CloneGroupTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.acct = make_account()
        self.hd = make_channel(self.acct, name='HD feed')
        self.sd = make_channel(self.acct, name='SD feed')
        _test(self.hd, '1920x1080', 60.0)
        _test(self.sd, '1280x720', 30.0)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_clone_channel_to_channel_starts_out_of_guide(self):
        src = make_group(name='Source', members=[self.hd], in_guide=True)
        db.session.commit()

        resp = self.t.client.post(f'/api/channel-groups/{src.id}/clone',
                                  json={'name': 'Source (copy)'})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        new_id = resp.get_json()['group_id']
        clone = db.session.get(ChannelGroup, new_id)
        self.assertFalse(clone.in_guide)
        self.assertEqual({m.channel_id for m in clone.memberships}, {self.hd.id})

    def test_clone_never_warns_on_a_format_mix(self):
        # The format question is asked of a group that RECORDS, and a clone records from
        # nobody - its members start with Recording off whatever strategy it is given -
        # so the question belongs to its promotion (dev/changelog/1077).
        src = make_group(name='Bag', members=[self.hd, self.sd],
                         in_guide=False, recording=False)
        db.session.commit()

        resp = self.t.client.post(f'/api/channel-groups/{src.id}/clone',
                                  json={'name': 'Bag (copy)',
                                        'format_strategy': 'highest_score'})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertTrue(resp.get_json()['success'])
        self.assertIsNotNone(ChannelGroup.query.filter_by(name='Bag (copy)').first())

    def test_clone_narrowed_by_channel_ids(self):
        src = make_group(name='Bag', members=[self.hd, self.sd],
                         in_guide=False, recording=False)
        db.session.commit()

        resp = self.t.client.post(f'/api/channel-groups/{src.id}/clone', json={
            'name': 'Narrowed', 'channel_ids': [self.hd.id],
        })
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        new_id = resp.get_json()['group_id']
        clone = db.session.get(ChannelGroup, new_id)
        self.assertEqual({m.channel_id for m in clone.memberships}, {self.hd.id})
        # Original untouched.
        self.assertEqual({m.channel_id for m in db.session.get(ChannelGroup, src.id).memberships},
                         {self.hd.id, self.sd.id})


class CreateCheckAttachedToGroupTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.acct = make_account()
        self.ch1 = make_channel(self.acct, name='Feed A')
        self.ch2 = make_channel(self.acct, name='Feed B')
        self.grp = make_group(name='Channel Group', members=[self.ch1, self.ch2], in_guide=True)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_attach_group_id_is_refused_and_mints_nothing(self):
        """dev/changelog/1077: the group already carries its one check."""
        before = OnDemandTestJob.query.count()
        resp = self.t.client.post('/api/channel-tests/on-demand', json={
            'name': 'Group check', 'action': 'queue', 'attach_group_id': self.grp.id,
        })
        self.assertEqual(resp.status_code, 409, resp.get_data(as_text=True))
        self.assertEqual(OnDemandTestJob.query.count(), before)
        # The group itself is unaffected - still exactly its own two members, and no
        # duplicate group spawned either.
        grp = db.session.get(ChannelGroup, self.grp.id)
        self.assertEqual(len(grp.memberships), 2)
        self.assertEqual(ChannelGroup.query.count(), 2)  # the system group + this one

    def test_attach_group_id_rejects_missing_group(self):
        resp = self.t.client.post('/api/channel-tests/on-demand', json={
            'name': 'Group check', 'action': 'queue', 'attach_group_id': 999999,
        })
        self.assertEqual(resp.status_code, 404)


class GroupsPageRenderTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_page_renders_the_one_groups_section(self):
        acct = make_account()
        ch = make_channel(acct, name='Feed', in_guide=True)
        make_group(name='A Channel Group', members=[ch], in_guide=True)
        db.session.commit()

        resp = self.t.client.get('/channel-groups')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn('A Channel Group', body)
        # One section now: a health check is a schedule a group carries, not a second
        # kind of object with a list of its own (DESIGN-channel-groups-model.md DECIDED 2).
        self.assertIn('id="grp-list-groups"', body)
        self.assertIn('TV Guide Channels', body)  # the pinned system group, in that list

    def test_empty_group_renders_without_error(self):
        make_group(name='Empty', members=[])
        db.session.commit()
        resp = self.t.client.get('/channel-groups')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Empty', resp.get_data(as_text=True))

    def test_inherited_only_check_chip_reads_as_coverage_not_a_schedule(self):
        """dev/docs/BUGS.md 2026-08-21 @ 09:18:58 PM ET: a group covered only by the
        inherited automatic TV Guide check rendered that check's own time/recurrence
        (e.g. "2:00 AM" or "Every day at 2:00 AM") as the group's own check chip, which
        reads as "this group is scheduled" when nothing was ever scheduled on the group
        itself. The chip must say it is inherited coverage, not a time or a recurrence
        description - that language is reserved for a schedule the group actually
        carries."""
        acct = make_account()
        ch = make_channel(acct, name='FS3', in_guide=True, test_enabled=True)
        make_group(name='FS3 Group', members=[ch], in_guide=True)
        db.session.commit()

        body = self.t.client.get('/channel-groups').get_data(as_text=True)
        row_idx = body.find('FS3 Group</span>')
        self.assertNotEqual(row_idx, -1, 'FS3 Group row not found')
        chunk = body[row_idx:row_idx + 4000]
        # The inherited chip names the SYSTEM job; the group's own check has a chip of its
        # own beside it now that every group carries one (dev/changelog/1077).
        sys_job = OnDemandTestJob.query.filter_by(is_system=True).one()
        grp = ChannelGroup.query.filter_by(name='FS3 Group').one()
        chip_attr = chunk.find(f'data-menu-check="{grp.id}:{sys_job.id}"')
        self.assertNotEqual(chip_attr, -1, 'FS3 Group has no inherited-coverage chip')
        tag_start = chunk.rfind('<span', 0, chip_attr)
        tag_close = chunk.find('>', chip_attr)
        label_end = chunk.find('</span>', tag_close)
        full_chip = chunk[tag_start:label_end + len('</span>')]
        visible_label = chunk[tag_close + 1:label_end]

        # The tooltip (kept in the full chip) may still explain the inherited job's own
        # time - only the *visible* label must stop looking like a schedule.
        self.assertIn('Inherited coverage', full_chip)
        self.assertEqual('Inherited coverage', visible_label)
        self.assertNotIn('&#8635;', visible_label)  # the recurring-schedule glyph
        self.assertNotIn('2:00', visible_label)  # the system job's own run time


# The `is_stored - ... / has_check - ...` lines of group_detail.html's file-level comment:
# six-space indent, a name, a dash, the description.
_DOC_FLAG = re.compile(r'^ {6}(\w+) - ', re.M)
# A model column named the way stale prose names one: `kind='channel'`, `is_system=1`.
_DOC_COLUMN_LITERAL = re.compile(r"(\w+)\s*=\s*'[^']*'")
_DOC_COLUMN_ATTR = re.compile(r'\bgroup\.(\w+)')


def _detail_page_doc_comment():
    """The `{# ... #}` block above `{% block content %}` in group_detail.html."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'templates', 'channels', 'group_detail.html')
    with open(path, encoding='utf-8') as fh:
        src = fh.read()
    start = src.index('{#', src.index('{% import'))
    return src[start:src.index('#}', start)]


class DetailPageDocCommentTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-08-19 - the unified detail page's own file-level comment
    described `is_stored` as "records with failover, appears in the guide (kind='channel')"
    a commit after `dev/changelog/741` deleted `ChannelGroup.kind` and moved recording to
    `format_strategy`/`recording_enabled`. The comment is the contract the page's ~25
    `{% if is_stored %}` gates are read against, so a wrong one is not cosmetic."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_the_comment_names_no_column_the_model_no_longer_has(self):
        doc = _detail_page_doc_comment()
        named = set(_DOC_COLUMN_LITERAL.findall(doc)) | set(_DOC_COLUMN_ATTR.findall(doc))
        missing = sorted(n for n in named if not hasattr(ChannelGroup, n))
        self.assertEqual([], missing,
                         'the comment documents the page against columns that do not '
                         'exist, so a reader gates on the wrong thing')

    def test_the_comment_documents_the_flags_the_route_really_passes(self):
        """Characterization, not a defect guard: it holds the prose and the context in
        step from here on, so a flag renamed in the route cannot leave the comment
        describing a name nothing passes."""
        acct = make_account()
        ch = make_channel(acct, name='Feed', in_guide=True)
        grp = make_group(name='Documented', members=[ch], in_guide=False)
        db.session.commit()

        contexts = []

        def record(sender, template, context, **extra):
            contexts.append(context)

        template_rendered.connect(record, self.t.app)
        try:
            resp = self.t.client.get(f'/channel-groups/{grp.id}')
        finally:
            template_rendered.disconnect(record, self.t.app)
        self.assertEqual(200, resp.status_code)

        keys = set().union(*(c.keys() for c in contexts))
        documented = set(_DOC_FLAG.findall(_detail_page_doc_comment()))
        self.assertTrue(documented, 'the comment stopped documenting any flag at all')
        self.assertEqual(set(), documented - keys)


if __name__ == '__main__':
    unittest.main(verbosity=2)
