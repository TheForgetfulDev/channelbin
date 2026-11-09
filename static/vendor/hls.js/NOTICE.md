# hls.js (vendored)

- Project: https://github.com/video-dev/hls.js
- Version: 1.7.3, `dist/hls.min.js` from the npm package `hls.js@1.7.3`, unmodified
- License: Apache License 2.0 - the full text is in `LICENSE` beside this file
- sha256 of `hls.min.js`: a12e7ee1cd64a69dcdb314157e45dafcba705bfb0b1440b7935cb265d374423e

Why it is here: browsers other than Safari have no native HLS playback, and the live channel
preview and the recording player (`app/preview.py`) serve HLS.

Why the full build and not the smaller `hls.light.min.js` (386 KB against 620 KB): the light
build refuses HEVC inside MPEG-TS outright ("Unsupported HEVC in M2TS found", a fatal
`fragParsingError`) before the browser's own decoder is asked, and every segment the app serves
is MPEG-TS. With the light build an HEVC channel or recording could not play even in a browser
that decodes HEVC. The extra 234 KB is paid once, on the first Preview or Play, never with the
page (dev/changelog/1461).

Upgrading: replace the two files from the new npm package, update the version and hash above.
Nothing in `static/js/` is patched against a specific hls.js version; `static/js/util.js::
loadHlsJs()` loads it on demand from the `data-hls-js` URL each player page hands its script.
