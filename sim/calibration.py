"""The calibration profile, read into the defaults a world starts from.

`profiles/default.yaml` holds aggregates measured from two real installs, kept apart under
`prod:` and `dev:` and never averaged. A world names which of the two it is built from
(`calibration: prod` unless it says otherwise), so every default below is one install's number,
chosen on purpose. Where the profile lists one value per account, the default is the median
account's value, which is still a measured number rather than a blend of two installs.
"""
import copy
import functools
import os
import statistics

import yaml

PROFILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'profiles', 'default.yaml')
SOURCES = ('prod', 'dev')

_RATE_KEYS = ('quality_mark', 'superscript_mark', 'tier_mark', 'pictographic_symbol',
              'separator_row', 'date_or_time', 'event_slot', 'non_latin_script', 'all_caps')


@functools.lru_cache(maxsize=4)
def _parsed(path: str) -> dict:
    with open(path, encoding='utf-8') as f:
        return yaml.safe_load(f)


def load_profile(path: str = PROFILE_PATH) -> dict:
    """The parsed profile. A copy, so a caller may change it without reaching the cache."""
    return copy.deepcopy(_parsed(os.path.abspath(path)))


def _median(values, default):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else default


class Calibration:
    """One install's half of the profile, as the plain numbers the generators need."""

    def __init__(self, source: str = 'prod', path: str = PROFILE_PATH):
        if source not in SOURCES:
            raise ValueError(f'calibration must be one of {", ".join(SOURCES)}, not {source!r}')
        profile = _parsed(os.path.abspath(path))
        if source not in profile.get('sources', {}):
            raise ValueError(f'the profile at {path} holds no {source!r} measurements')
        self.source = source
        self.profile_measured = profile.get('measured')
        names = profile['names']
        self.channel_prefix_style = dict(names['channel']['prefix_style'][source])
        self.channel_rates = {k: names['channel'][k][source] for k in _RATE_KEYS}
        self.channel_length = dict(names['channel']['length_chars'][source])
        self.category_prefix_style = dict(names['category']['prefix_style'][source])
        self.category_rates = {k: names['category'][k][source] for k in _RATE_KEYS}

        accounts = profile['lineup']['accounts'][source]
        self.tvg_id_coverage = _median([a.get('tvg_id_coverage') for a in accounts], 0.5)
        self.logo_coverage = _median([a.get('logo_coverage') for a in accounts], 0.99)
        self.channels_per_category = _median(
            [(a.get('channels_per_category') or {}).get('p50') for a in accounts], 40)
        self.same_name_stem_share = _median([a.get('same_name_stem_share') for a in accounts], 0.3)
        self.tvg_id_shared_share = _median(
            [a.get('tvg_id_shared_by_several_channels_share') for a in accounts], 0.3)
        self.stream_url_shape = dict(profile['lineup']['stream_url_shape'][source])

        guide = profile['guide']
        self.guide_coverage_of_tvg_ids = guide['coverage_of_channels_with_tvg_id'][source]
        self.program_minutes = dict(guide['program_minutes'][source])
        self.filler_share = guide['filler_share_2min_or_less'][source]
        self.long_program_share = guide['six_hours_or_longer_share'][source]
        self.gap_rate = guide['gap_over_1min_rate'][source]
        self.overlap_rate = guide['overlap_rate'][source]
        self.same_title_rate = guide['same_title_as_previous_rate'][source]
        self.days_ahead = guide['days_ahead_of_sync'][source]['p50']
        present = guide['field_present_share']
        files = profile['guide_files']['field_present_share']
        # Sub-titles, descriptions, categories and ratings were measured per install from the
        # imported listings; icons, episode numbers and <new> only exist in the raw files, which
        # were read once from the dev box and are not split by install.
        self.field_share = {
            'sub_title': present['sub_title'][source],
            'description': present['description'][source],
            'category': present['category'][source],
            'rating': present['rating'][source],
            'icon': files['icon'],
            'episode_num': files['episode-num xmltv_ns'],
            'new': files['new'],
        }
        self.guide_offsets = dict(profile['guide_files']['offset'])

        elsewhere = profile.get('measured_elsewhere', {})
        self.buffer_burst_seconds = float(
            elsewhere.get('buffer_burst_on_connect_seconds', {}).get('typical', 25))

        formats = profile['formats']
        self.formats = {k: dict(formats[k][source]) for k in
                        ('resolution', 'frame_rate', 'video_codec', 'audio_codec', 'audio_channels')}
