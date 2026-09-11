# Changelog

Notable changes to ChannelBin, newest first. This project follows
[semver](https://semver.org): pre-1.0, MINOR releases may include breaking changes. Every
release is tagged `v<version>` in git, and the version the app is running is shown in the
page footer.

## 0.6.0 - 2026-09-11

**Added**

- Maintenance has an External tools card showing which ffmpeg and ffprobe the app is actually
  using, their versions and where each was found, plus the ffmpeg components ChannelBin relies
  on and whether this build has them. A standing alert is raised when either tool cannot be
  run.
- ffprobe has its own path setting beside ffmpeg's. Left blank, it follows a configured ffmpeg
  to the ffprobe beside it, so pointing the app at a separate ffmpeg build moves both tools
  together. Both paths expand a leading `~`.

**Changed**

- ChannelBin no longer bundles an ffmpeg through pip: `ffmpeg` and `ffprobe` must both be
  installed. The app targets the ffmpeg 7.1 series, and the Docker image fails to build rather
  than ship a different one.

**Fixed**

- The Docker image could not start in any earlier release: a config write failed with a
  permission error before the app served a request.
- With ffmpeg present but ffprobe missing, channel tests and recordings reported the empty
  result as a fault in the stream. They now say the tool is missing.
- 4K channels that are not HDR were tonemapped as if they were, darkening their screenshots by
  about a third, and HDR sources that labeled only part of their color information fell back to
  an untonemapped screenshot.

## 0.5.1 - 2026-09-10

**Added**

- Scheduling a recording against a channel group now says which member it would use. The
  record modal names that member and its account, and says the choice can change before the
  recording starts and again while it is running, so a group no longer looks identical to a
  single channel while meaning something different.

**Fixed**

- The record modal's footer overflowed the panel on a phone, putting Cancel and Schedule
  recording out of reach.
- The Search Programs (EPG) landing page had slowed as channel group membership grew, because
  it ranked group members over every showing on screen even when the page held none of them.
- The test suite failed instead of skipping on a machine with no ffmpeg installed.

## 0.5.0 - 2026-09-09

**Added**

- A channel's health score can be rolled back by hand from the Channel Health card: a full
  reset that returns it to no score at all, and a repeatable step back that unwinds the single
  most recent thing that moved it. Nothing is deleted to unwind a score - the observation
  behind it is excluded instead, and stays on the channel's timeline marked as not counted, so
  a recording is never destroyed to correct a number.
- A channel group's members can be filtered by EPG id, and the member table can show it as a
  column. The EPG-mismatch banner's Review members button now lands on the members it is
  warning about, rather than on the recording-enabled set.

**Changed**

- Dashboard now leads the main menu, above any section heading. The Channels section is
  retired and its two entries moved into Library, as Channel Search and Channel Groups.

**Fixed**

- HEVC channels always screenshotted as a blank gray frame, which left every one of them
  carrying an unearned "screenshot appears solid color" warning and a health score depressed
  by it - and, since health score is how a group ranks its members, permanently demoted in
  group selection. Screenshots and live recording thumbnails now decode from a keyframe.
- A channel test's frames-received percentage measured the expected frame count against the
  container duration rather than the video stream's decode span, so a complete capture read as
  incomplete and the shorter the test the worse it looked. Healthy high-frame-rate channels
  were losing health score for frames that were never missing.

## 0.4.1 - 2026-09-09

**Added**

- Recordings from a channel group now move to another member when the one they are on keeps
  stalling, even when every restart succeeds. Previously only a feed that stopped answering
  triggered failover, so a feed that stalled constantly and always came back never did. The
  trigger is a rate (by default 3 stalls within 30 minutes, configurable globally and per
  recording profile), and the member being left is demoted rather than dropped, so it stays
  selectable if nothing better is available.

**Fixed**

- Deleting a recording whose conversion had failed left the partial output file and the
  conversion scratch files on disk, with nothing in the app pointing at them. For a long
  recording that could be several gigabytes. Both are now removed with the rest of the
  recording.
- A channel group's format lock could be won by a format that none of the members the group
  actually records from had, because the ranking counted every member rather than the
  recording-enabled ones.
- The format strategy picker previewed a different set of health check results than the
  engine ranked on, so its preview could disagree with the lock that was applied.
- The "Healthiest member's format" option was labeled with the group's existing lock instead
  of that member's measured format, and a bucket whose channels were all in warning was
  described as healthy.
- A bucket that cannot be ranked now dims, as the rest of the list already did.

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
