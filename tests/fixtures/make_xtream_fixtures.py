"""Regenerate the scrubbed live/VOD classification fixtures from real provider dumps.

Not run by the test suite - this is the provenance record for the committed fixtures in
this directory, kept so a future maintainer can rebuild them from a fresh dump instead of
reverse-engineering what the shapes were meant to be.

EVERY host, username and password is replaced with a blatantly fake value on a reserved
`.invalid` TLD (RFC 2606 - guaranteed never to resolve). Nothing here may contain a real
provider domain, credential, channel name or stream id.

Channel names and stream ids used to be kept verbatim, on the reasoning that they are not
secret. They are not secret, but together they fingerprint the account they came from: the
lineup, the provider's internal id numbering and its distinctive name decorations
identified one specific provider and marked its subscriber (dev/changelog/713). Both are
now neutralized, and the tests are unaffected because none of them asserts on a name and
none asserts on an id VALUE - what `mixed_cuid` exists to prove is that a CUID matches the
catalog while the URL's numeric tail does not, which is a relationship, not a number.

Two rules a future edit must keep, both learned the hard way:

  * Replace whole labels, never just the domain. Appending `.invalid` to a real subdomain
    leaves the real part standing, and a reserved TLD makes it look handled.
  * Scrub the ENCODED forms too. A percent-encoded `https%3A%2F%2F` inner URL sailed
    through a scrubber that matched only literal `https://`.

Usage:  python3 tests/fixtures/make_xtream_fixtures.py
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

OUT = os.path.dirname(os.path.abspath(__file__))

# Blatantly fake replacements. Deliberately unmistakable if one ever leaks into a log.
FAKE_USER = 'FAKEUSER0000'
FAKE_PASS = 'FAKEPASS0000'

# Added to EVERY provider-supplied id - CUID, catalog stream_id and the URL's numeric tail
# alike - so none of the real numbering survives. It must be applied to all three or to
# none: a uniform offset is a bijection, so every equality the fixtures depend on holds
# exactly as before (rootless_portal's tail == stream_id, mixed_cuid's CUID == stream_id
# while tail != stream_id). Offsetting only some of them would silently break one of those
# relationships and take a real regression guard down with it.
ID_OFFSET = 4271993

# Invented channel-name stems. Names are replaced rather than kept, but the REPLACEMENT
# preserves the original's decoration characters (see scrub_name) - the emoji, the Unicode
# superscripts, the `###` padding and the `XX|` language prefixes are real parsing edge
# cases and are the reason these fixtures are worth having.
FAKE_STEMS = (
    'NORTHSTAR', 'BLUE RIDGE', 'CEDAR', 'ATLAS', 'MERIDIAN', 'HARBOR', 'PINNACLE',
    'LANTERN', 'SUMMIT', 'DRIFTWOOD', 'IRONWOOD', 'CASCADE', 'BEACON', 'JUNIPER',
    'SANDSTONE', 'WILLOW', 'FOXGLOVE', 'MARLIN', 'TIMBER', 'ORCHARD',
)

SOURCES = [
    {
        'name': 'rootless_portal',      # acct 3 shape
        'dump': 'instance/xtream-dumps/3/2026-07-22_01',
        'portal': 'http://fake-portal-rootless.invalid:80',
        'real_hosts_to_fake': {'default': 'fake-cdn.invalid'},
        'limit': 40,
    },
    {
        'name': 'mixed_cuid',           # acct 2 shape
        'dump': 'instance/xtream-dumps/2/2026-07-14_01',
        'portal': 'http://fake-portal-mixed.invalid',
        'real_hosts_to_fake': {'default': 'fake-cdn-mixed.invalid'},
        'limit': 40,
    },
]


# Generic industry vocabulary, kept verbatim in channel names. None of it identifies a
# provider - every playlist on earth carries these - and keeping them is what stops the
# scrubbed names reading as noise.
KEEP_WORDS = frozenset({
    'K', 'HD', 'FHD', 'UHD', 'SD', 'TV', 'P', 'EVENT', 'ONLY', 'LIVE', 'NEW', 'VIP',
    'EN', 'DE', 'ES', 'FR', 'IT', 'PL', 'PT', 'US', 'UK', 'CA', 'AU', 'NL', 'SE',
    # Kept so shift_ids can still find `dummy-<n>` placeholder EPG ids after the names
    # have been scrubbed. Renaming the prefix would strand the real id behind it.
    'DUMMY',
})

_stem_by_word = {}


def scrub_name(name: str) -> str:
    """Replace the identifying words in a channel/group/EPG name, keep the shape.

    Every run of two or more ASCII letters that is not generic industry vocabulary maps to
    an invented stem; digits, emoji, Unicode superscripts, `###` padding, pipes, colons and
    spacing all survive byte-for-byte. That is deliberate - the decorations are real parser
    edge cases (and the reason these fixtures earn their place), while the words are what
    identify the provider's lineup.

    The mapping is stable within a run, so a family of related channels stays a family
    rather than fragmenting into unrelated names.
    """
    def repl(m):
        word = m.group(0)
        if word.upper() in KEEP_WORDS:
            return word
        key = word.upper()
        if key not in _stem_by_word:
            _stem_by_word[key] = FAKE_STEMS[len(_stem_by_word) % len(FAKE_STEMS)]
        stem = _stem_by_word[key]
        # Match the original's case so LOUD names stay loud and Titled names stay titled.
        if word.isupper():
            return stem
        if word[0].isupper():
            return stem.title()
        return stem.lower()

    return re.sub(r'[A-Za-z]{2,}', repl, name)


def scrub_extinf(ext: str) -> str:
    """Scrub every display string on an #EXTINF line: the tvg-name/tvg-id/group-title
    attributes and the trailing comma-separated display name. Everything else on the line,
    CUID included, is left for shift_ids."""
    for attr in ('tvg-name', 'tvg-id', 'group-title'):
        ext = re.sub(rf'({attr}=")([^"]*)(")',
                     lambda m: f'{m.group(1)}{scrub_name(m.group(2))}{m.group(3)}', ext)
    head, sep, display = ext.rpartition(',')
    return f'{head}{sep}{scrub_name(display)}' if sep else ext


def shift_ids(text: str) -> str:
    """Apply ID_OFFSET to every provider id in a rendered fixture: CUID attributes, catalog
    stream_ids, `dummy-<n>` EPG placeholders, and the numeric tail of a stream URL.

    Done as a final pass over the rendered text rather than inline, so the playlist and the
    catalog are matched up using the provider's own ids first and only then renumbered
    together - the correspondence between the two files is what the tests actually assert.
    """
    text = re.sub(r'(CUID=")(\d+)(")',
                  lambda m: f'{m.group(1)}{int(m.group(2)) + ID_OFFSET}{m.group(3)}', text)
    text = re.sub(r'("stream_id":\s*)(\d+)',
                  lambda m: f'{m.group(1)}{int(m.group(2)) + ID_OFFSET}', text)
    text = re.sub(r'(dummy-)(\d+)',
                  lambda m: f'{m.group(1)}{int(m.group(2)) + ID_OFFSET}', text, flags=re.I)
    # category_id arrives as either a JSON string or a bare number, depending on provider.
    text = re.sub(r'("category_id":\s*"?)(\d+)',
                  lambda m: f'{m.group(1)}{int(m.group(2)) + ID_OFFSET}', text)
    # A stream URL's trailing id, with or without an extension: /604331 or /604331.ts
    text = re.sub(r'(/)(\d+)(\.[a-z0-9]+)?(?=["\s]|$)',
                  lambda m: f'{m.group(1)}{int(m.group(2)) + ID_OFFSET}{m.group(3) or ""}',
                  text, flags=re.M)
    return text


def scrub_url(url: str, portal_host: str, cdn_host: str, portal_real_host: str) -> str:
    """Replace host and credential path segments; keep path SHAPE and numeric ids."""
    m = re.match(r'^([a-z0-9+.-]+)://([^/]+)(/.*)?$', url, re.I)
    if not m:
        return url
    scheme, host, path = m.group(1), m.group(2), m.group(3) or ''
    bare = host.lower().rsplit(':', 1)[0] if ':' in host else host.lower()
    new_host = portal_host if bare == portal_real_host.lower() else cdn_host
    # Credential-looking segments are long alnum tokens; replace positionally so the
    # /user/pass/<id> vs /live/user/pass/<id>.ts distinction survives intact.
    segs = path.split('/')
    seen = 0
    for i, s in enumerate(segs):
        # Hyphens and underscores are part of the token, not a boundary. Requiring bare
        # alphanumerics once let a real 16-character CDN path token through a scrub
        # untouched: the single hyphen in it was enough to fail the match
        # (dev/changelog/713).
        if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{7,}', s) and not s.isdigit():
            segs[i] = FAKE_USER if seen == 0 else FAKE_PASS
            seen += 1
    path = '/'.join(segs)
    # Any surviving query string may carry tokens - drop it entirely.
    path = path.split('?')[0]
    return f'{scheme}://{new_host}{path}'


def make_catalog_only():
    """The account-4 shape: the provider's playlist endpoint is unavailable (HTTP 884), so
    only the JSON catalog exists and NO entry carries a URL. Every stream URL therefore has
    to be constructed. Emitted as a dump dir with no live_streams_m3u.m3u, which is exactly
    what FileXtreamClient sees for such a provider."""
    root = os.path.dirname(os.path.dirname(OUT))
    dump = os.path.join(root, 'instance/xtream-dumps/4/2026-07-22_01')
    if not os.path.isdir(dump):
        print('SKIP catalog_only: no account-4 dump present')
        return
    rows = json.load(open(os.path.join(dump, 'live_streams_json_api.json'), encoding='utf-8'))
    slim = [{
        'stream_id': r.get('stream_id'),
        'name': scrub_name(r.get('name', '')),
        'stream_type': r.get('stream_type', 'live'),
        'epg_channel_id': scrub_name(r.get('epg_channel_id', '')),
        'category_id': r.get('category_id'),
        'direct_source': '',          # empty on every row for this provider - the whole point
    } for r in rows[:30]]
    base = os.path.join(OUT, 'catalog_only')
    os.makedirs(base, exist_ok=True)
    text = shift_ids(re.sub(r'\b(https?)://[^\s"\',]*', 'https://fake-logos.invalid/logo.png',
                            json.dumps(slim, indent=1, ensure_ascii=False)))
    with open(os.path.join(base, 'live_streams_json_api.json'), 'w', encoding='utf-8') as f:
        f.write(text)
    print(f'catalog_only: {len(slim)} catalog rows, no playlist file (by design)')


def main():
    root = os.path.dirname(os.path.dirname(OUT))
    for src in SOURCES:
        dump = os.path.join(root, src['dump'])
        if not os.path.isdir(dump):
            print(f'SKIP {src["name"]}: no dump at {dump}')
            continue
        raw = open(os.path.join(dump, 'live_streams_m3u.m3u'), encoding='utf-8',
                   errors='replace').read().splitlines()
        catalog = json.load(open(os.path.join(dump, 'live_streams_json_api.json'),
                                 encoding='utf-8'))

        from urllib.parse import urlparse
        portal_real_host = (urlparse(
            json.load(open(os.path.join(dump, 'dump_meta.json'), encoding='utf-8'))['base_url']
        ).hostname or '')
        portal_host = urlparse(src['portal']).netloc
        cdn_host = src['real_hosts_to_fake']['default']

        # Walk the playlist, keeping a spread of shapes rather than the first N entries:
        # rootless, /live/…ts, VOD-path, and external-CDN entries all need representation.
        buckets = {'rootless': [], 'live_ts': [], 'vod': [], 'external': []}
        i = 0
        while i < len(raw):
            if raw[i].startswith('#EXTINF:'):
                ext = raw[i]
                j = i + 1
                while j < len(raw) and (not raw[j].strip() or raw[j].startswith('#')):
                    j += 1
                if j < len(raw):
                    url = raw[j].strip()
                    p = urlparse(url)
                    low = p.path.lower()
                    # Bucket by URL SHAPE, and compare hostnames WITHOUT the port.
                    # Comparing full netloc mis-buckets every account-3 entry as
                    # 'external', because its portal carries :80 and its base_url has no
                    # port (dev/changelog/713).
                    same_host = (p.hostname or '').lower() == portal_real_host.lower()
                    has_num_id = bool(re.search(r'/(\d+)(?:\.[a-z0-9]+)?$', p.path))
                    if '/movie/' in low or '/series/' in low:
                        b = 'vod'
                    elif not has_num_id and not same_host:
                        b = 'external'
                    elif '/live/' in low:
                        b = 'live_ts'
                    else:
                        b = 'rootless'
                    if len(buckets[b]) < src['limit'] // 4:
                        buckets[b].append((ext, url))
                i = j
            i += 1

        kept = [e for b in buckets.values() for e in b]
        lines = ['#EXTM3U']
        kept_ids = set()
        for ext, url in kept:
            new_url = scrub_url(url, portal_host, cdn_host, portal_real_host)
            lines.append(scrub_extinf(ext))
            lines.append(new_url)
            c = re.search(r'CUID="(\d+)"', ext)
            if c:
                kept_ids.add(c.group(1))
            else:
                m = re.search(r'/(\d+)(?:\.[a-z0-9]+)?$', urlparse(new_url).path)
                if m:
                    kept_ids.add(m.group(1))

        # Catalog: only rows for kept ids, plus scrubbed direct_source for external ones.
        ds_by_url = {}
        for ext, url in buckets['external']:
            ds_by_url[url] = scrub_url(url, portal_host, cdn_host, portal_real_host)
        slim = []
        for row in catalog:
            sid = str(row.get('stream_id'))
            ds = row.get('direct_source') or ''
            if sid in kept_ids or ds in ds_by_url:
                slim.append({
                    'stream_id': row.get('stream_id'),
                    'name': scrub_name(row.get('name', '')),
                    'stream_type': row.get('stream_type', 'live'),
                    'epg_channel_id': scrub_name(row.get('epg_channel_id', '')),
                    'category_id': row.get('category_id'),
                    'direct_source': ds_by_url.get(ds, '') if ds else '',
                })

        # Final blanket pass: every host that is not one of our own fake ones becomes
        # fake-logos.invalid. Catches tvg-logo / stream_icon, which are third-party image
        # CDNs but also, on one real account, the provider's own logo server at a bare IP.
        # Anything identifying gets scrubbed, not just the obvious stream URLs.
        fake_hosts = {portal_host, cdn_host}

        def _scrub_foreign_hosts(text: str) -> str:
            """Replace foreign URLs whole - host AND path AND query.

            A host-only rewrite is not enough, and both ways it failed are recorded in
            dev/changelog/713: a logo path kept a real GitHub account's repo route intact
            under a fake host, and a percent-encoded `https%3A%2F%2F` inner URL was never
            seen at all because the pattern only matched a literal `https://`. Foreign
            URLs are therefore flattened to a bare host with a neutral path, and the
            encoded form is matched explicitly. No test asserts on a logo URL - only on
            stream URLs, which `scrub_url` handles above - so flattening costs nothing.
            """
            def repl(m):
                if m.group(2) in fake_hosts:
                    return m.group(0)
                return f'{m.group(1)}://fake-logos.invalid/logo.png'
            # Consume the whole URL - path and query included - not just the host prefix.
            text = re.sub(r'\b(https?)://([^/\s"\',]+)[^\s"\',]*', repl, text)
            # And the percent-encoded spelling, which the pattern above cannot see.
            text = re.sub(r'https?%3A%2F%2F[^\s"\',&]*', 'https%3A%2F%2Ffake-logos.invalid',
                          text, flags=re.I)
            return text

        base = os.path.join(OUT, src['name'])
        os.makedirs(base, exist_ok=True)
        # shift_ids runs last, over the rendered text of both files at once, so the
        # playlist and the catalog are renumbered by the same offset in the same pass.
        with open(os.path.join(base, 'live_streams_m3u.m3u'), 'w', encoding='utf-8') as f:
            f.write(shift_ids(_scrub_foreign_hosts('\n'.join(lines))) + '\n')
        with open(os.path.join(base, 'live_streams_json_api.json'), 'w', encoding='utf-8') as f:
            f.write(shift_ids(_scrub_foreign_hosts(
                json.dumps(slim, indent=1, ensure_ascii=False))))
        print(f'{src["name"]}: {len(kept)} playlist entries '
              f'({ {k: len(v) for k, v in buckets.items()} }), {len(slim)} catalog rows')


if __name__ == '__main__':
    main()
    make_catalog_only()
