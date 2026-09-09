# Changelog

Notable changes to ChannelBin, newest first. This project follows
[semver](https://semver.org): pre-1.0, MINOR releases may include breaking changes. Every
release is tagged `v<version>` in git, and the version the app is running is shown in the
page footer.

## 0.4.0 - 2026-09-08

Initial public release.

Everything below already existed before this tag - the app was developed privately and this
is the first version anyone else can install. There is no earlier public release to compare
against, so this section describes what you get rather than what changed.

**Recording**

- Scheduled recordings by channel, group, or raw stream URL, one time or recurring.
- Stall detection with automatic ffmpeg restart, then concatenation of every segment into a
  single output file. Stall timeout, restart delay, and failure ceiling are all configurable.
- Fast-fail handling for a stream that never really starts, and failover to the next-highest
  scored member of a group when the active feed dies mid-recording.
- Post-processing to mp4 or mkv, with re-encode triggered only when the capture is measurably
  damaged (or always, or never).
- Filename templates with a live preview, an optional move to a finished directory, an
  optional post-recording script, named recording profiles, and an optional retention sweep.

**Visibility into what happened**

- A live dashboard with elapsed and remaining time, bytes captured, stall and failure counts,
  and a periodically refreshed thumbnail of the frame being recorded right now.
- A per-recording event log covering every stall, restart, failover, and conversion step, with
  timestamps and reasons, plus a per-segment size and duration breakdown.
- Capture diagnostics stored alongside the recording, so a failed capture can say why.
- Alerts, with optional push delivery through Apprise service URLs, rate limited.

**Channels, guide, and search**

- Xtream and M3U/XMLTV accounts synced on a schedule, with sync logs, per-account connection
  limits, and a sync that stands aside while a recording is running or about to start.
- A scrollable multi-day TV Guide built from the imported EPG, with indicators on programs
  that already have a recording scheduled.
- Channel search and airing search across the whole catalog, with filters, facets, and saved
  columns, built to stay responsive on catalogs well past a hundred thousand channels.
- An EPG browser, channel tags, and groups that collapse duplicate feeds of one logical
  channel into a single guide entry ranked by health score.
- Health checks that connect to a channel, measure resolution and bitrate against configurable
  thresholds, capture screenshots, and feed a rolling per-channel health score.

**Operations**

- Docker image and bare-metal install, both driven by one `config.yaml`.
- Automatic startup migration of the config file and the database on upgrade.
- Automatic config and database backups, a log viewer, an in-app settings editor, a jobs page,
  a maintenance page, and a sanitized support bundle for sharing diagnostics without sharing
  credentials.
- Optional password gate for the whole app, and a read-only Home Assistant integration.
