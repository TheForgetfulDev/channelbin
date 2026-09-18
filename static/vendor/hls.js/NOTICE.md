# hls.js (vendored)

- Project: https://github.com/video-dev/hls.js
- Version: 1.7.3, `dist/hls.light.min.js` from the npm package `hls.js@1.7.3`, unmodified
- License: Apache License 2.0 - the full text is in `LICENSE` beside this file
- sha256 of `hls.light.min.js`: 0251332c00a216a35d7d6919044d60da82b78beb3bd50ac6c662ae74a2f5474b

Why it is here: browsers other than Safari have no native HLS playback, and the live channel
preview (`app/preview.py`) serves HLS. The "light" build is used because the preview's
playlists are produced by this app's own ffmpeg and never carry alternate audio, subtitles,
DRM or content steering - the features the light build leaves out.

Upgrading: replace the two files from the new npm package, update the version and hash above.
Nothing in `static/js/` is patched against a specific hls.js version; the page loads it on
demand from `static/js/channel-detail.js` when Preview is first clicked.
