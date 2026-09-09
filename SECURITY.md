# Security

## Reporting a vulnerability

Please report security issues privately through GitHub's built-in reporting flow rather than
opening a public issue: go to the **Security** tab of this repository and choose **"Report a
vulnerability"**. That opens a private advisory that only you and the maintainer can see until
it's resolved. (If you don't see that option, it means the maintainer hasn't finished enabling it
yet - open a normal issue asking for a private contact instead, without describing the
vulnerability in it.)

This is a one-person hobby project, so there's no SLA on response time, but reports are read and
taken seriously.

## Threat model

ChannelBin is a single-user, self-hosted app meant to run on your home network, not on the open
internet. A few specifics worth knowing:

- **Network binding.** The default bind address is `0.0.0.0`, meaning it listens on every network
  interface on the machine it runs on, not just `localhost`. The app itself warns about this at
  startup ("reachable from any device on this network") - it's a documented posture, not a bug.
  If you expose it beyond your LAN, put it behind a reverse proxy with TLS.
- **Authentication is optional and minimal.** There's one shared password for the whole app -
  disabled by default, no usernames, no roles, no two-factor. It's meant as a basic gate on a
  door that would otherwise stand wide open, not a hardened login system. Every route except the
  login/logout pages and static assets is gated once auth is turned on. Failed login attempts are
  rate-limited per IP address.
- **Session cookies** are always `HttpOnly` and `SameSite=Lax`. The `Secure` flag is off by
  default and should be turned on (`auth.cookie_secure: true` in `config.yaml`) if you're serving
  the app over HTTPS.
- **Home Assistant integration** uses a separate, randomly generated API key (not the shared
  password), stored as a hash rather than in plaintext.
- **Support bundles** (Settings > Backup & Restore > Support Bundle) strip credentials and
  redact URLs before writing anything to disk, so they're safe to attach to a public issue. If
  you're reporting a bug rather than a vulnerability, a support bundle is the preferred way to
  share logs and config - please don't paste raw `dvr.log` or `config.yaml` into a public issue.

## Supported versions

Only the latest released version is supported. There's no long-term support branch.
