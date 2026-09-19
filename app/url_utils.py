"""Canonical home for stream/fetch URL credential masking.

IPTV URLs carry the account's username/password inline - as Xtream path segments
(`/live/<user>/<pass>/<id>`), as query params (`?username=&password=`), or as HTTP
userinfo (`http://user:pass@host/`). Anything that renders one in the UI, writes one
to dvr.log, or persists one to an error column must run it through here first.

Two entry points for URLs of *unknown* provenance, deliberately different:
  mask_creds(url)          - the whole string is one URL (Jinja filter, explicit log args)
  mask_creds_in_text(text) - a URL may be embedded in prose or a traceback

They differ because the bare-<user>/<pass>/<id> rule below is ^…$-anchored, so it only
fires when the URL is the entire string. mask_creds_in_text extracts URL tokens first
precisely so that anchored rule still applies inside a longer message.

Two more for URLs the caller *knows* are account-owned (an Account's own m3u_url /
epg_url / base_url), where the whole path is secret and no heuristic can prove it:
  mask_url_path(url)                    - host kept, path+query replaced wholesale
  mask_account_urls_in_text(text, *urls) - the same, for URLs embedded in prose

See DESIGN-secrets.md §4.2: for path-token providers (https://host/TESTPATHTOKEN1) the
path IS the credential, so an account-owned URL is secret in full at every call site
that logs, persists or exports it. The heuristics above stay the backstop for stream
URLs, whose provenance the CredentialMaskingFilter structurally cannot know.

One more, categorically blunter than all of the above - for the support bundle export
only (app/support_bundle.py), which might leave this machine:
  redact_urls_in_text(text) - ANY <scheme>://... token, scheme kept, everything after
  it replaced. No host survives, no credential-shape judgment call is made.
"""
import logging
import re

log = logging.getLogger(__name__)

# URL token inside arbitrary text. Excludes whitespace and the quote/bracket characters
# that normally delimit a URL in log output and Python reprs.
_URL_IN_TEXT_RE = re.compile(r'https?://[^\s\'"<>()\[\]]+')

# The request target urllib3 prints in a MaxRetryError: a path, never a full URL. It is
# masked by lending it a throwaway origin so every mask_creds() rule applies unchanged.
_REQUEST_TARGET_RE = re.compile(r'(\burl: )(/[^\s\'"<>()\[\]]*)')
_TARGET_ORIGIN = 'http://request-target.invalid'

# Punctuation that commonly trails a URL in prose ("...from http://h/a/b/c.") and would
# otherwise defeat the $-anchored bare-path rule.
_TRAILING_PUNCT = '.,;:!?'


def mask_creds(url):
    """Mask IPTV credentials in a single URL. Returns non-URL input unchanged."""
    if not url:
        return url
    # userinfo: scheme://user:pass@host
    url = re.sub(r'(?i)^(https?://)[^/@\s]+@', r'\1***:***@', url)
    # /live/, /movie/, /series/ <user>/<pass>/<id> path style (standard Xtream endpoints)
    url = re.sub(r'(/(?:live|movie|series)/)([^/]+)/([^/]+)(/)', r'\1***/***\4', url)
    # username=/password= (or user=/pass=) query params
    url = re.sub(r'(?i)((?:username|password|user|pass)=)[^&]+', r'\1***', url)
    # "Bare" <user>/<pass>/<id> path style with no /live/ prefix - common for
    # CDN-redirected Xtream stream URLs (scheme://host/<user>/<pass>/<id>[.ext]).
    # Anchored to the whole path (exactly 2 slashes after the host) so it can't
    # accidentally eat a longer, non-credential path.
    url = re.sub(r'^(https?://[^/]+/)([^/]+)/([^/]+)/([^/]+?)(\?.*)?$', r'\1***/***/\4\5', url)
    return url


def mask_creds_in_text(text):
    """Mask IPTV credentials in every URL found inside a larger string.

    Used for log messages, exception tracebacks (requests embeds the full credentialed
    URL in its exception text), and any error string persisted to the DB.

    Also masks the scheme-less request target urllib3 names when a host cannot be
    reached (`Max retries exceeded with url: /player_api.php?username=U&password=P`),
    which carries the same credentials with no `://` in front of them.
    """
    if not text:
        return text
    if 'url: /' in text:
        text = _REQUEST_TARGET_RE.sub(
            lambda m: m.group(1) + mask_creds(_TARGET_ORIGIN + m.group(2))[len(_TARGET_ORIGIN):],
            text)
    if '://' not in text:
        return text

    def _replace(match):
        token = match.group(0)
        # Split trailing sentence punctuation off before masking, so the anchored
        # bare-<user>/<pass>/<id> rule in mask_creds() still sees a clean URL.
        tail = ''
        while token and token[-1] in _TRAILING_PUNCT:
            tail = token[-1] + tail
            token = token[:-1]
        return mask_creds(token) + tail

    return _URL_IN_TEXT_RE.sub(_replace, text)


def mask_url_path(url):
    """Mask an account-owned URL: keep scheme+host, replace the whole path and query.

    `https://example-provider.test/TESTPATHTOKEN1` -> `https://example-provider.test/***`. Use this - never
    mask_creds - wherever the URL is known to be an Account's own m3u_url/epg_url/
    base_url, because for path-token providers the path itself is the credential and
    mask_creds' shape heuristics cannot recognize it (DESIGN-secrets.md §4.2).

    Returns non-URL input unchanged, and leaves a URL with no path alone (nothing to hide).
    """
    if not url:
        return url
    # userinfo first, so credentials in the authority are masked even though the
    # authority itself is preserved below
    url = re.sub(r'(?i)^(https?://)[^/@\s]+@', r'\1***:***@', url)
    return re.sub(r'(?i)^(https?://[^/?#\s]+)[/?#][^\s]+$', r'\1/***', url)


_ANY_SCHEME_URL_RE = re.compile(r'([A-Za-z][A-Za-z0-9+.\-]*)://[^\s\'"<>()\[\]]+')

REDACTED_URL_TEXT = '[url redacted]'


def redact_urls_in_text(text):
    """Replace every <scheme>://... token in `text` with '<scheme>://[url redacted]' -
    the scheme survives, nothing else does. Deliberately blunter and scheme-agnostic
    (matches ANY scheme, not just http(s)) unlike every other function in this module,
    which preserve the host for on-screen/log diagnostics that never leave this
    machine. This is for the support bundle export only (app/support_bundle.py) - an
    artifact that might leave the machine, where DESIGN-secrets.md's "even the host can
    be sensitive" call means nothing here tries to judge which URLs matter.
    """
    if not text or '://' not in text:
        return text

    def _replace(match):
        scheme = match.group(1)
        full = match.group(0)
        tail = ''
        while full and full[-1] in _TRAILING_PUNCT:
            tail = full[-1] + tail
            full = full[:-1]
        return f'{scheme}://{REDACTED_URL_TEXT}{tail}'

    return _ANY_SCHEME_URL_RE.sub(_replace, text)


def mask_account_urls_in_text(text, *urls):
    """Mask account-owned URLs inside prose/traceback text, then apply the heuristics.

    `requests` exceptions stringify with the full URL that was fetched, and those strings
    are persisted (account.last_error, AccountSyncLog.error_message), rendered in the UI
    and pushed off-box as alerts - so the caller, which is the only thing that knows those
    URLs are account-owned, substitutes them here before mask_creds_in_text runs as the
    generic backstop for anything else in the message.

    Longest first: a base_url is often a prefix of the m3u_url built from it, and replacing
    the short one first would leave the rest of the longer path exposed.
    """
    if not text:
        return text
    urls = {u for u in urls if u}
    for url in sorted(urls, key=len, reverse=True):
        text = text.replace(url, mask_url_path(url))
    # The same URL's path on its own, as urllib3 names it when the host is unreachable -
    # for a path-token provider that path is the whole credential.
    paths = {_path_and_query(u) for u in urls} - {'', '/'}
    for path in sorted(paths, key=len, reverse=True):
        text = text.replace(path, '/***')
    return mask_creds_in_text(text)


def _path_and_query(url):
    match = re.match(r'(?i)^https?://[^/?#\s]+([/?#]\S*)?$', url)
    return (match.group(1) or '') if match else ''


class CredentialMaskingFilter(logging.Filter):
    """Last line of defense: mask credentials on every record before it reaches a handler.

    Explicit masking at call sites is still preferred (it keeps intent visible), but this
    catches what call sites structurally cannot: `requests` exceptions stringify with the
    full credentialed URL, so every `log.warning(..., exc)` and `log.exception(...)` in the
    sync path leaks without it - including future ones nobody remembers to mask.

    Must be attached to each *handler*, not to the root logger: a logger's filters only run
    for records originating at that logger, not for records propagating up from child
    loggers. Attaching it to the alert handler is what keeps credentials out of outbound
    push notifications (_AlertHandler -> create_alert -> enqueue_push).
    """

    # Own formatter instance, used only to render exc_info -> text. Never used to format
    # the record itself; that stays the handler's job.
    _exc_formatter = logging.Formatter()

    def filter(self, record):
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            # Malformed %-args - leave the record alone rather than mangling it; the
            # handler will surface the formatting error on its own.
            return True
        masked = mask_creds_in_text(message)
        if masked != message:
            record.msg = masked
            record.args = None
        if record.exc_info and not record.exc_text:
            # Pre-populating exc_text is how the traceback gets masked: Formatter.format()
            # reuses a already-set exc_text instead of re-rendering exc_info itself.
            record.exc_text = mask_creds_in_text(
                self._exc_formatter.formatException(record.exc_info))
        return True
