"""Tier 0 - static invariants for the container packaging (dev/changelog/517).

The image itself cannot be exercised here: the suite has no Docker daemon and
tests/support/netguard.py forbids the network a build would need. What *can* be guarded is
the part where a mistake is expensive and silent, and both of these were verified by
actually building and running the image before this file was written:

  * **The leak guard.** A `config.yaml` or `instance/` copied into the build context is
    baked into an image layer, readable by anyone who pulls the image - and this app's
    config.yaml carries plaintext provider credentials, notification webhook tokens and the
    Flask secret key (DESIGN-secrets.md). `.dockerignore` is the only thing standing between
    the two, and nothing else in the tree would notice if a line were dropped from it.

  * **The persistence guard.** The app pins config.yaml and instance/ to its own directory
    (app/config.py::_CONFIG_PATH, app/__init__.py::_resolve_secret_key), so the image
    symlinks both into the /config volume. If a symlink or a path in
    docker/config.docker.yaml drifts to somewhere outside a mounted volume, the container
    still starts and still works - and silently discards the database, the settings or the
    session key at the next image upgrade. That is exactly the class of failure this app
    exists to make loud rather than discover later.

  * **The substitution guard** (dev/changelog/908). A floating base tag makes the shipped
    ffmpeg an accident of whichever Debian release the tag currently tracks, and that already
    moved the image's capture engine from ffmpeg 5.1 to 7.1.5 with no commit. The version
    itself is asserted inside the build, which no unittest can reach; what is guarded here is
    that the assertion and the release-qualified tag are still present in the Dockerfile.
"""
import os
import re
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCKERIGNORE = os.path.join(ROOT, '.dockerignore')
DOCKERFILE = os.path.join(ROOT, 'Dockerfile')
COMPOSE = os.path.join(ROOT, 'docker-compose.example.yml')
DOCKER_CONFIG = os.path.join(ROOT, 'docker', 'config.docker.yaml')
ENTRYPOINT = os.path.join(ROOT, 'docker', 'entrypoint.sh')

# The two volume mount points the image declares. Any path the container must not lose
# across an image upgrade has to live under one of them.
VOLUMES = ('/config', '/dvr')


def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def _ignore_patterns():
    return [ln.strip() for ln in _read(DOCKERIGNORE).splitlines()
            if ln.strip() and not ln.strip().startswith('#')]


class DockerignoreLeakTests(unittest.TestCase):
    """Nothing secret-bearing may enter the build context."""

    # Each entry is (pattern that must be excluded, what leaks without it).
    MUST_EXCLUDE = [
        ('config.yaml', 'provider credentials, webhook tokens and flask.secret_key'),
        ('instance/', 'the generated Flask secret key and the DB/config backups'),
        ('dvr.db', 'the whole database, including plaintext account passwords'),
        ('capture-logs/', 'ffmpeg stderr spools, which can quote a credentialed stream URL'),
        ('*.log', 'the application log'),
        ('.git/', 'the full history, which predates config.yaml being untracked'),
    ]

    def test_secret_bearing_paths_are_excluded_from_the_build_context(self):
        patterns = _ignore_patterns()
        missing = [f'{pat} ({why})' for pat, why in self.MUST_EXCLUDE if pat not in patterns]
        self.assertEqual(
            missing, [],
            '.dockerignore no longer excludes a secret-bearing path, so `docker build` would '
            'bake it into an image layer where every puller can read it:\n  '
            + '\n  '.join(missing))

    def test_what_the_container_runs_is_not_excluded(self):
        """The inverse mistake: excluding something the ENTRYPOINT needs at runtime.

        A build that drops docker/ or app/ produces an image that fails on first start
        rather than at build time, so nothing catches it until someone runs it.
        """
        patterns = _ignore_patterns()
        required = ('app/', 'app', 'docker/', 'docker', 'run.py', 'templates/', 'static/')
        excluded = [p for p in required if p in patterns]
        self.assertEqual(
            excluded, [],
            '.dockerignore excludes something the running container needs: '
            + ', '.join(excluded))


class DockerReadmeTests(unittest.TestCase):
    """README.md must not enter the build context.

    It references docs/screenshots/*.png by relative path, and docs/ is (correctly)
    excluded from the image - so a README that ships anyway renders with seven broken
    <img> tags for anyone reading it inside a running container or an extracted image.
    Nothing in the running app ever reads README.md, so excluding it costs nothing.
    """

    def test_readme_is_excluded_from_the_build_context(self):
        self.assertIn(
            'README.md', _ignore_patterns(),
            '.dockerignore no longer excludes README.md, so it would ship inside the image '
            'with seven broken docs/screenshots/*.png references (docs/ is excluded).')


class DockerPersistencePathTests(unittest.TestCase):
    """Everything that must survive an image upgrade lives on a mounted volume."""

    def test_hardcoded_app_paths_are_symlinked_into_the_config_volume(self):
        dockerfile = _read(DOCKERFILE)
        for target, link in (('/config/config.yaml', '/app/config.yaml'),
                             ('/config/instance', '/app/instance')):
            self.assertIn(
                f'ln -s {target} {link}', dockerfile,
                f'The image no longer symlinks {link} to {target}. The app resolves that path '
                'relative to its own directory and has no environment-variable seam for it, so '
                'without the symlink it lands in the container layer and is lost on upgrade.')

    def test_every_configured_path_lives_on_a_volume(self):
        cfg = yaml.safe_load(_read(DOCKER_CONFIG))
        strays = []
        for section, values in cfg.items():
            if not isinstance(values, dict):
                continue
            for key, value in values.items():
                if not isinstance(value, str) or not value.startswith('/'):
                    continue
                if not value.startswith(VOLUMES):
                    strays.append(f'{section}.{key} = {value}')
        self.assertEqual(
            strays, [],
            'docker/config.docker.yaml points at an absolute path outside the mounted volumes '
            f'{VOLUMES}, so whatever is written there is discarded on the next image '
            'upgrade:\n  ' + '\n  '.join(strays))

    def test_the_compose_example_mounts_both_volumes(self):
        compose = yaml.safe_load(_read(COMPOSE))
        mounts = compose['services']['channelbin']['volumes']
        targets = [m.split(':')[1] for m in mounts]
        for vol in VOLUMES:
            self.assertIn(vol, targets,
                          f'docker-compose.example.yml stops mounting {vol}, so a user who '
                          'copies it loses that data whenever the container is recreated.')

    def test_the_entrypoint_never_walks_the_recordings_volume(self):
        """/dvr is routinely a multi-terabyte share. A recursive chown of it at every
        container start costs minutes of startup and rewrites files the user may share with
        other apps - the entrypoint warns about a bad ownership instead of "fixing" it."""
        for line in _read(ENTRYPOINT).splitlines():
            code = line.split('#')[0]
            if 'chown' in code and '-R' in code:
                self.assertNotIn('/dvr', code,
                                 'The entrypoint recursively chowns /dvr: ' + line.strip())


class DockerFfmpegPinTests(unittest.TestCase):
    """The image's ffmpeg may not change without a commit (dev/changelog/908).

    ffmpeg is the one external tool this app's output depends on, and the container is the
    only environment where ChannelBin picks it. Every behavior the codebase treats as settled
    about ffmpeg was measured against one build, so a version that arrives by accident is a
    silent substitution of the capture engine - which is what happened when Debian 13 went
    stable and the image's ffmpeg jumped from 5.1 to 7.1.5 with nothing noticing.
    """

    # Debian release codenames a base tag may name. A tag carrying none of them is floating,
    # whatever else it says.
    DEBIAN_RELEASES = ('bookworm', 'trixie', 'forky', 'sid')

    def _from_tag(self):
        match = re.search(r'^FROM\s+(\S+)', _read(DOCKERFILE), re.MULTILINE)
        self.assertIsNotNone(match, 'Dockerfile has no FROM line.')
        return match.group(1)

    def test_the_base_image_tag_names_a_debian_release(self):
        """`python:3.12-slim` follows whatever Debian release the tag tracks, so the ffmpeg
        under it changes major version on Debian's schedule rather than on a commit."""
        tag = self._from_tag()
        self.assertTrue(
            any(rel in tag for rel in self.DEBIAN_RELEASES),
            f'The base image is `{tag}`, which names no Debian release, so the Debian version '
            'under it - and therefore the ffmpeg series apt installs - can move without a '
            'commit. Pin a release-qualified tag (e.g. python:3.12-slim-trixie).')

    def test_the_ffmpeg_series_is_declared_exactly_once(self):
        """Two declarations is two sources of truth for which ffmpeg the image contains, and
        a container smoke test reads this value to know what to expect inside the image."""
        declarations = re.findall(r'^ARG\s+FFMPEG_SERIES=(\S+)', _read(DOCKERFILE),
                                  re.MULTILINE)
        self.assertEqual(
            len(declarations), 1,
            'The Dockerfile must declare FFMPEG_SERIES exactly once (found '
            f'{len(declarations)}): it is the single statement of which ffmpeg this image '
            'ships, and what the build assertion and any smoke test compare against.')
        self.assertRegex(
            declarations[0], r'^\d+\.\d+$',
            f'FFMPEG_SERIES is `{declarations[0]}`, not a major.minor series. An exact patch '
            'version would fail the build on every Debian security update; a bare major would '
            'let a series move through unnoticed.')

    def test_the_build_asserts_the_series_for_both_binaries(self):
        """The app needs ffmpeg and ffprobe both, and a missing ffprobe is silent at runtime -
        every probe returns an empty dict, with no error and no event."""
        dockerfile = _read(DOCKERFILE)
        self.assertIn(
            '${FFMPEG_SERIES}', dockerfile,
            'FFMPEG_SERIES is declared but never referenced, so nothing in the build checks '
            'the version it names and the declaration is decoration.')
        self.assertIn(
            'for bin in ffmpeg ffprobe', dockerfile,
            'The build no longer checks the resolved version of both binaries. Checking only '
            'ffmpeg leaves a missing or mismatched ffprobe to be discovered at runtime, where '
            'it reports nothing at all.')
        self.assertIn(
            'exit 1', dockerfile,
            'The version check can no longer fail the build, so a substituted ffmpeg ships '
            'with a warning in a build log nobody reads.')


class DockerConfigKeysTests(unittest.TestCase):
    """A typo in the seeded config is silent - unknown keys are ignored, and the app just
    runs on the default the line was written to override."""

    def test_every_seeded_key_is_a_key_the_app_actually_reads(self):
        from app.config import _DEFAULTS
        cfg = yaml.safe_load(_read(DOCKER_CONFIG))
        unknown = []
        for section, values in cfg.items():
            if section not in _DEFAULTS:
                unknown.append(section)
                continue
            if not isinstance(values, dict):
                continue
            for key in values:
                if key not in _DEFAULTS[section]:
                    unknown.append(f'{section}.{key}')
        self.assertEqual(
            unknown, [],
            'docker/config.docker.yaml sets a key app/config.py::_DEFAULTS does not define, '
            'so it is silently ignored and the container runs on the default it meant to '
            'override: ' + ', '.join(unknown))


if __name__ == '__main__':
    unittest.main()
