# Contributing to ChannelBin

Before you start reading the code, see [docs/CONVENTIONS.md](docs/CONVENTIONS.md) - it explains the
documents that comments and test docstrings cite, which are not part of this repository.

## Commit messages

Commits use [Conventional Commits](https://www.conventionalcommits.org/): a `type:` prefix
followed by a short summary.

```
feat: add failover to a group's next-priority account
fix: stop watchdog from restarting a segment that already completed
chore: bump ffmpeg invocation flags for HLS streams
docs: clarify Docker volume layout in the README
refactor: extract stream URL construction into accounts.py
```

Common types: `feat`, `fix`, `chore`, `docs`, `refactor`, `test`. Scope is optional
(`fix(recorder): ...`). Keep the summary line under ~72 characters; put detail in the body if
the change needs explaining.

## Linting

[`flake8`](https://pypi.org/project/flake8/) catches unused imports, undefined names, and unused
local variables. `.flake8` restricts it to `E9,F` - the [`pyflakes`](https://pypi.org/project/pyflakes/)
checks plus syntax errors - so it has no style opinions and will never argue with you about line
length or whitespace. It's a dev-only, on-demand tool: `.github/workflows/tests.yml` runs the test
suite on every push and pull request but no lint step, and there's no git hook, so run it yourself
whenever you want:

```
pip install -r requirements-dev.txt
flake8 app/ run.py tools/ tests/ docker/
```

A clean run prints nothing and exits 0. Any output is a real finding worth fixing.

Some imports exist purely for their side effects and are correctly reported as unused by a tool
that only reads the file in front of it - `app/__init__.py`'s `from . import database` registers
the ORM models on `db.metadata` before `db.create_all()`/migrations run. Those carry a
`# noqa: F401` with a note saying why, which flake8 honors. Add one the same way if you write
another; don't delete the import.

## Versioning and tags

The app version lives in `app/version.py` (`__version__`) and follows semver - see that file's
own docstring for the pre-1.0 vs. post-1.0 convention, which this document doesn't restate.

Every published release gets a git tag matching the version at the time of publish
(`v0.3.0`, etc.). A release is never left untagged, and a tag is never reused - if you're
preparing a release, bump `app/version.py` first. The publish tooling enforces this: it
refuses to publish if the current version's tag already exists, rather than silently skipping
the tag or double-tagging a commit.
