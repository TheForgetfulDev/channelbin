# Changelog

Notable changes to ChannelBin, newest first. This project follows
[semver](https://semver.org): pre-1.0, MINOR releases may include breaking changes. Every
release is tagged `v<version>` in git, and the version the app is running is shown in the
page footer.

## 0.16.1 - 2026-09-22

Fixes a test that could fail on a loaded machine for reasons unrelated to what it was
testing. That failure is what kept 0.16.0 from being published, so nothing in the app
itself changed between the two: this is the release to install for everything listed
under 0.16.0 below.

## 0.16.0 - 2026-09-22

**Changed**

- The Home Assistant integration now lives in its own repository,
  [channelbin-homeassistant](https://github.com/TheForgetfulDev/channelbin-homeassistant), and is
  installed from there.
- One width now decides the phone layout, 960px. A desktop window or a tablet narrower than that
  gets the phone arrangement where it used to get a mix of the two.

**Fixed**

- A phone held in landscape mode gets the phone layout. The app switched its shell and its
  components at two different widths, and a landscape phone landed between them, so wide tables
  reflowed for neither.
- README fixes for the Home Assistant integration.

## 0.15.0 - 2026-09-22

**Added**

- Search Programs (EPG) says how many channels its matching airings are on, not just how many
  airings there are.

**Changed**

- A channel group can no longer have duplicate health checks. Clone is one screen with a switch
  for copying the schedule and profile, the Pick best format modal is gone, and "this group
  records" now means at least one member has Recording turned on.
- A group's health check shows one "Test now" button in every state except for running.

**Fixed**

- The schedule picker for a group's health check opens on the maintenance window instead of a
  specific time.
- The Dashboard's accounts and health sections keep themselves current on a page left open.
- The Search index card on Maintenance notices a rebuild that starts after the page loads, which
  is how every account sync ends.
- The TV Guide's recording chip stops saying Recorded while a recording's segments are still
  being joined and checked.
- The Dashboard timeline legend, four Settings fields and an account's activity line say a
  clashing job is deferred, which is what happens to it, rather than skipped.
- The group Settings modal no longer prints "undefined" under its Format explanation or leaves
  the recording setup button floating mid-panel.

## 0.14.0 - 2026-09-21

**Added**

- A running health check says how much longer it has left, on the group page and on the
  Dashboard.
- Channel search can filter on whether a health check watches a channel, so you can find the
  holes in your monitoring.
- Each account on the Dashboard shows its health signal: average score, how many of its
  channels are failing now, and a band bar.
- Account is a real column on a channel group's member table, carrying the provider's name and
  not just a colored dot, sortable and reorderable like every other column.

**Changed**

- The automatic TV Guide Channels health check can be unscheduled, like every other check.
  Where the app used to advertise that check's incidental coverage of a group, it now says No
  coverage rather than naming a schedule that will never fire.
- The Drops column on a group's member table is off by default.

**Fixed**

- The Dashboard's live timeline advances with the clock instead of freezing at the time the
  page was loaded.
- An account with auto-sync turned off says off under Next sync, instead of a bare dash.

## 0.13.0 - 2026-09-20

**Added**

- A finished recording can write an .nfo file and a poster image next to the video, so Plex,
  Jellyfin, Emby and Kodi show its real title, synopsis, air date, genre and rating instead of
  guessing from the filename. Off by default, with a per-profile override.
- Recordings keep their own copy of the program's synopsis, genre and rating, shown in a
  Program card on the recording page, so the details survive the guide data being pruned.
- An Edit details button on that card lets you correct the title, synopsis, genre and rating
  before they reach the sidecar, lock them against being refreshed, and turn the sidecar off
  for one recording.
- A recording profile can carry its own poster image, uploaded in the profile's edit modal and
  used as the artwork for every recording made under it.
- The poster frame for a finished recording is taken from inside the program itself, 60 seconds
  past its real start by default, so front padding cannot put a countdown clock or the previous
  show on the cover.
- The TV Guide's record modal opens with a full program header: title, subtitle, when it airs
  and how long, the channel, the whole description and matched tags. Tapping a program on a
  phone goes straight there.
- Channel search has a rows-per-page menu of 100, 250 or 500, and a setting for the size it
  opens with.
- The raw config.yaml editor in Settings has a Validate button, and Save refuses text it can
  see is broken instead of taking every page down.
- The unmonitored-members warning on a channel group can be hidden per group, like the group's
  other warnings.
- The channel detail page links the account that owns the channel.

**Changed**

- The Home Assistant integration declares the oldest ChannelBin it runs against. Against an
  older server its sensors go unavailable and Home Assistant shows a repair issue naming both
  versions.
- The Readiness alerts check counts unread errors only, matching the Alerts page, and is
  advisory: it no longer turns the headline red or puts a badge on the Maintenance menu.

**Fixed**

- Deleting a recording removes everything it wrote, including a kept .ts source and
  ChannelBin's own thumbnail.
- The recording page shows the running segment's real size and rate while it captures, instead
  of 0 B.
- A cancelled recording's page says what the cancel actually left on disk, instead of reporting
  the capture as discarded while offering to convert it.
- The Dashboard stops counting down a conversion that is paused behind a live recording, and
  says what it is waiting on.
- The logo cache job is only scheduled when the feature is on, instead of ticking every five
  minutes to do nothing.
- Pre-migration database snapshots are written next to the database they back up.
- A channel group's Updated date moves when you add or remove channels, change a member's
  switches, or edit its health check.
- The group page's Add Matching Channels list marks channels the provider has stopped sending,
  the same way every other list does.
- The Maintenance menu's readiness counts are red and amber instead of grey.
- The desktop sidebar is wide enough for its own menu labels.

## 0.12.0 - 2026-09-18

**Added**

- The Accounts list compares your accounts side by side: a line of live numbers under each
  account, and a comparison over 7, 30 or 90 days or all time with a sortable table, share
  charts and trend charts.
- Each account's page has a Usage section with recorded time, recordings, stalls, failovers and
  health checks over a chosen window.

**Changed**

- Editing the times of a scheduled group recording picks the member that should serve the new
  window, and switches to one on an account with a free connection instead of warning about a
  clash it could avoid.
- Restart no longer refuses over a recording whose conversion is waiting on another recording.
  It names those recordings and what a restart costs them.
- Inside a container, the Restart confirm warns that the container needs a restart policy to
  come back up.

**Fixed**

- Account usernames and passwords no longer appear in the Readiness login check, or in sync
  errors, alerts and the log when a provider host cannot be reached.
- Recorded time under failover is credited to the account that captured each part, not all to
  the account the recording ended on.
- The Restart dialog notices the app is back after a fast container restart instead of waiting
  forever.
- Dashboard badges follow each recording's real status: a finished capture stops showing
  Recording, a cancelled one stops showing live, and Waiting now appears.
- A "waiting for a connection slot" alert clears when that recording fails to start.
- URLs with a trailing slash open the page instead of a 404.
- The Readiness check that the database accepts writes catches a read-only database, and the
  sync line reports the oldest sync's age as its label says.
- A few pages and API calls answer with a clear error instead of a server error when given
  malformed input.

## 0.11.0 - 2026-09-18

**Added**

- A preview button that allows you to play a live stream of any channel so you can verify it
  works and that it is what your EPG says it is. Note that channel health checks are still the
  ideal (and automated) way to verify a channel functions, but the preview option lets you
  actually watch it. Also note this is NOT designed as an actual IPTV viewing application, it's
  just to preview a channel.
- The app regularly checks every folder it is set to write to and alerts you if any fail.
- The support bundle includes the Readiness report.
- The Home Assistant integration can be installed through HACS as a custom repository.

**Changed**

- All images (recording thumbnails, health check screenshots, and cached channel logos) now share
  one images folder. Each one is stored in their own subfolder, but it makes managing that
  directory much easier.

**Fixed**

- The thumbnail folder can be changed in Settings. Before, an install that could not write to the
  default folder had no way to stop thumbnails failing.
- Channel health scores give each health check its full weight again. A default check had been
  counting as half an observation since checks were shortened to 30 seconds. Existing scores are
  unchanged.
- Rebuilt some tables that required horizontal scrolling when viewing from a phone.

## 0.10.0 - 2026-09-17

**Added**

- Settings opens in a Basic view with the settings a new user is most likely to need (or want) to
  change, and a Basic/Advanced switch. Search still finds every setting from either view.
- Every setting changed from its default is marked, and a filter shows only those.
- A setting that has no effect while a switch or mode is off fades and names what it depends on.
  Settings that can be overridden by a profile, channel, or account have an indicator and a link
  to where that override can be set.
- Recordings can read a stream at real-time speed, as a default or per channel, and switch to it
  on their own when a provider replays its buffer after a reconnect.
- A published container image at `ghcr.io/theforgetfuldev/channelbin`, which the example compose
  file now uses, and an Unraid Community Apps template.

**Changed**

- The capture read timeout now defaults to 20 seconds instead of 5, so a stream is no longer
  dropped during the normal wait between a provider's chunks. An install that set its own value
  keeps it.
- Settings fills the page width, and the larger cards group their fields by what they control.

**Fixed**

- Ctrl/Cmd-click and middle-click on list rows, dashboard tiles and program titles open a new tab
  instead of replacing the page.
- Several "Default:" lines on the Settings page showed the wrong value, and some descriptions no
  longer matched what the app does. Both now match the app.

## 0.9.3 - 2026-09-16

**Fixed**

- Cancelling a recording while it was starting could leave its capture running with nothing able
  to stop it, and after a restart a recording could be started twice, with two captures writing
  the same file. A start now claims the recording before doing anything, so either one owner
  starts it or the cancel wins.
- A recording that was still retrying a dead stream when its time ended was decided by whichever
  of two jobs ran first, and one of them failed the recording without saving what had already
  been captured. Anything captured is now always joined into a file; a recording fails as a dead
  stream only when nothing was captured at all.
- A crash, restart or out-of-memory kill during a segment join left the join's ffmpeg running
  and writing the output file while the next start began a second join beside it. The join is
  now tracked like every other capture, stopped at shutdown, and its partial file removed.
- A crash at the very end of a resumable conversion could leave it believing its work was still
  to do, and the next attempt then deleted the finished file and failed the recording. The
  finished file is now recorded before anything is cleaned up, and a complete, readable file
  already in place is kept rather than destroyed.
- Most failed recordings said "the stream could not be reached" whatever actually went wrong.
  Every way a recording can fail now records its real cause, the recording page says it, and it
  offers only the recovery actions that can work - a join that would refuse the same clips again
  is no longer offered.
- A stream restart that could not launch ffmpeg was counted twice, so a recording could give up on
  its stream sooner than it should have. A recording that has already failed is also no longer
  failed a second time, which had counted the failure against its channel's health score twice.
- A recording with no channel - recorded from a URL, or whose channel was later deleted - never
  recorded that its post-capture analysis had finished, so every restart read the whole file
  again and reported an earlier check as unfinished.
- Recomputing a channel's health score could arrive at a different number from the one stored,
  because a recording's observations were replayed in the wrong order. Each observation now keeps
  the moment it was applied, and a replay reproduces the stored score.

## 0.9.2 - 2026-09-15

Published as a container image only; these changes first reach this repository with 0.9.3.

**Fixed**

- The Logs page works in the Docker image, where it had two separate problems. One file shipped
  without read permission for the unprivileged user the container runs as, so the page loaded
  but its script did not, and it sat on "Loading history..." forever. And containers logged only
  to standard output, so even with the script loaded there was no log file to show. The image
  now normalizes file permissions as it is built, so no file can arrive unreadable again, and
  containers write a log file as well, leaving `docker logs` unchanged. An existing container is
  updated automatically on its first start after upgrading - nothing to edit by hand.
- A Logs page with no log file configured now says so and names the setting, instead of showing
  an empty page with no explanation.

**Changed**

- The support bundle now records how the install is deployed - whether it is running in a
  container, and as which user. A permission error means completely different things on a normal
  install and in a container, and nothing in the bundle previously said which one it came from.

## 0.9.1 - 2026-09-15

**Changed**

- Internal only, with no change to how the app behaves. Code that had been duplicated without
  being reused correctly was cleaned up, and code comments that had not been updated as the
  code changed were brought current.

## 0.9.0 - 2026-09-14

**Added**

- A feed that freezes or loops is now caught while the recording is still running. ChannelBin
  watches how much video is arriving against how much time is passing, and stops a capture that
  has been running faster than real time, rather than letting it fill hours with one repeated
  lap of the same content.
- The two phases that used to look hung now report progress. Joining a recording's segments
  shows percent, how many segments have been joined, elapsed time and an estimate; the
  post-capture analysis names which pass is reading and how far through the file it has got.
  Both appear on the recording, in the recordings list and on the Dashboard.
- A finished segment that delivered more video than the time it ran for is named in plain
  language on the recording's timeline and flagged on the recording itself. Nothing is thrown
  away - it is there to be judged before you sit down to watch.

**Fixed**

- Some providers answer a dead channel with a finite black "channel offline" clip instead of
  dropping the connection. ChannelBin recorded it: the clip drained in seconds, looked like an
  ordinary restart, and was fetched and joined again and again, putting hours of black in the
  middle of a recording. Such a segment is now recognized, kept out of the join and out of every
  coverage figure, reported loudly on the recording, and counted against the channel that served
  it. A health check spots the same thing.
- A provider that stopped sending bytes while holding the connection open cost a killed capture,
  a fresh connection and a new segment. The capture now times the dead socket out and reconnects
  inside the same segment, and reports how many times it had to.
- A health check that failed while probing a provider's offline placeholder was defining that
  channel's format, which filtered the channel out of every recording in its group. Only a
  completed check now sets a channel's format, and a group member row that shows one check's
  numbers while judging on another says which check it used.
- Damage detection weighted each segment by how long it took to arrive rather than by how much
  video it holds, so recordings that arrived faster than real time were reported damaged and
  sent into full re-encodes.
- The same recording could read "Joining" on one page and "Concatenating" on another. Every
  surface now uses one name per status.
- A second app start-up deleted the capture log of a recording that was already running, so the
  reason a segment ended could be lost with nothing saying so. Only the process that owns
  startup recovery sweeps those logs now, and a lost log is reported on the recording instead of
  looking like silence.

## 0.8.0 - 2026-09-14

**Added**

- Readiness check. It verifies your install to make sure everything is healthy and properly
  set up.
- A conversion that is waiting on an active recording moves to a paused state instead of
  acting like it's doing something.
- A scheduled sync that a recording blocked is caught up at the first free moment instead of
  waiting a whole interval, and an account that falls a full interval behind raises an alert
  that clears itself.
- The Activity chip lists each running task as its own clickable row, and the chip itself
  opens the tooltip rather than navigating away.

**Changed**

- The netted "Content missing" figure is replaced by two independently measured ones: wall
  clock when nothing was capturing, and how the finished file compares against the time the
  capture actually ran.

**Fixed**

- A conversion that had to step aside for a recording was killed and restarted from the
  beginning, losing hours of encoding. It is now suspended in place and continues where it
  left off.
- A conversion killed by a stall, a crash or a restart re-encoded the whole file from the
  start. It now resumes from the last completed part.
- Joining a recording's segments was killed by a fixed timeout even while it was writing at
  full speed. It is now supervised on progress, and a failed join no longer leaves a partial
  file behind.
- Restarting the app with a recording mid-analysis re-ran the whole analysis and counted it
  into the channel's health score a second time.
- The check that decides when a conversion yields to a recording used the recording's whole
  length rather than the work left, and did not count a recording's own post-capture work as
  a conflict.

## 0.7.1 - 2026-09-12

**Fixed**

- An alert for a failed conversion or file move could stand forever under Active alerts, where
  no Dismiss is offered, even after the recording had converted or moved successfully. Those
  alerts are now re-examined when the app starts and taken down once it can see the work
  completed, with a log line naming what it saw.
- The alert banner above every page now says when the alert was raised, so an alert from last
  week no longer reads exactly like one that just happened. The full date and the age are on
  hover and in the details view.

## 0.7.0 - 2026-09-12

**Added**

- The Alerts page splits into two cards. Active alerts holds the ones the app takes down by
  itself once the condition clears, and offers no Dismiss anywhere, so a problem that is still
  true cannot be waved away and forgotten. Past alerts holds everything else.
- The alert counter in the menu shows errors and warnings as separate counts rather than one
  total, and informational notes are no longer counted at all. The banner shows the most severe
  unread alert instead of the newest, with one-click Mark read, and its details view moves
  "Ignore future alerts like this" one level down.
- The account page shows how many entries each sync skipped: stream URLs with no scheme, and
  stream ids repeated within one feed. The counts appear on the last finished sync, on each
  line of the sync history, and in the accounts list tooltip.
- The accounts list refreshes itself while a sync is running, instead of showing SYNCING and
  stale counts until the page is reloaded by hand.
- The Hide Rules page says when a rule pass was refused because other work was running, and a
  skipped account sync is recorded in that account's own sync history.

**Changed**

- Twelve kinds of alerts are no longer raised. Each reported a fact that is not a problem, and
  each is now shown on the group, recording, account or page it concerns. Existing alerts of
  those kinds are cleared on upgrade.
- A group format warning appears only when the format was pinned by hand and a member with
  Recording on measures something else. Previously every group whose members spanned formats
  wore a red badge and a banner, counted members that were not recording, and claimed that
  mixed formats break failover, which they do not.
- A group's automatic format is settled once at the end of a health check run, over complete
  data, rather than re-decided after every single channel test over a half-updated picture.

**Fixed**

- Deleting a recording left its alerts attached to the row number, which was then reissued to
  the next recording created, so those alerts silently re-attached to an unrelated recording
  and deep-linked to it. A recording's number is now retired when it is deleted, and the
  alerts are unlinked on the way out.
- Failed syncs, file moves, concatenations and conversions reached the Alerts page only as
  untyped application errors that nothing could ever clear, and doubled up with the typed alert
  for the same event. Each now has its own kind and clears when the failure recovers.
- The alerts for a recording waiting on a connection slot and for a channel group in the TV
  Guide with no recording member stayed up after the condition had cleared. Both now take
  themselves down, and the guide-row alert links to the group it names.
- The database snapshot taken before a schema migration was an empty file rather than a copy of
  the database being migrated.
- Tooltips printed a literal character code in the middle of a sentence instead of breaking the
  line.

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
