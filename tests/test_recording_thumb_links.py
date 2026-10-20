"""The recordings list and a recording's page link a screenshot only when the screenshot route
will serve one (dev/docs/BUGS.md 2026-10-19 07:28 AM).

Found by the UI suite (dev/changelog/1226): with recording.live_thumbnail.enabled off, an
IN_PROGRESS recording's page and row still linked /recordings/<id>/live-thumbnail.jpg, which
answers 404 while the feature is off, so every load and every 15 s swap made a request that
failed and the page showed "NO SCREENSHOT".
"""
import os
import unittest

from app import db
from app.database import (REC_STATUS_COMPLETED, REC_STATUS_IN_PROGRESS,
                          REC_STATUS_RETRYING)
from tests.support import make_test_app, seed


class ThumbLinkTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.addCleanup(self.t.cleanup)
        with self.t.app.app_context():
            acc = seed.make_account()
            ch = seed.make_channel(acc, stream_id=1, name='Ch')
            self.live = seed.make_recording(status=REC_STATUS_IN_PROGRESS, channel_id=ch.id).id
            self.done = seed.make_recording(status=REC_STATUS_COMPLETED, channel_id=ch.id).id
            self.retrying = seed.make_recording(status=REC_STATUS_RETRYING, channel_id=ch.id).id
            db.session.commit()

    def _write_thumb(self, rid):
        from app.config import load_config
        from app.storage_dirs import THUMBNAILS, image_dir
        folder = image_dir(load_config(), THUMBNAILS)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, f'{rid}.jpg'), 'wb') as fh:
            fh.write(b'jpg')

    def _linked(self, path, rid):
        page = self.t.client.get(path).get_data(as_text=True)
        return f'/recordings/{rid}/live-thumbnail.jpg' in page

    def test_nothing_links_a_screenshot_while_the_feature_is_off(self):
        self.t.sandbox_config({'recording': {'live_thumbnail': {'enabled': False}}})
        self._write_thumb(self.done)
        for rid in (self.live, self.done):
            self.assertFalse(self._linked(f'/recordings/{rid}', rid), rid)
            self.assertFalse(self._linked('/recordings', rid), rid)
            self.assertEqual(self.t.client.get(f'/recordings/{rid}/live-thumbnail.jpg')
                             .status_code, 404)

    def test_with_the_feature_on_the_links_match_what_the_route_serves(self):
        self.t.sandbox_config({'recording': {'live_thumbnail': {'enabled': True}}})
        self._write_thumb(self.done)
        self.assertTrue(self._linked(f'/recordings/{self.done}', self.done))
        self.assertTrue(self._linked('/recordings', self.done))
        self.assertTrue(self._linked('/recordings', self.live))
        # Retrying with no image on disk: the route has nothing to serve, so no link.
        self.assertFalse(self._linked('/recordings', self.retrying))
        self.assertEqual(self.t.client.get(f'/recordings/{self.retrying}/live-thumbnail.jpg')
                         .status_code, 404)


if __name__ == '__main__':
    unittest.main()
