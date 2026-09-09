# Conventions in this repo

## Comments that cite documents you do not have

If you read much of this code you will run into comments and test docstrings pointing at files this
repository does not contain. They look like this:

```python
# hoisted per request, never per row (CLAUDE.md "no hidden I/O in per-row loops")
```

```python
"""Guards dev/docs/BUGS.md 2026-07-18: FAILED recordings never concatenate, so a duration
read off the output file is wrong for exactly the recordings that need it most."""
```

Those files are real. They live in the private repo I develop this app in, and they do not ship
here. This page says what they are, so a reference you cannot follow reads as a deliberate choice
rather than a broken link.

## What gets cited

- **CLAUDE.md and AGENTS.md** - the coding standards. Canonical homes for shared helpers, the rules
  around database writes and subprocess handling, and the defect classes that keep coming back. The
  same rules written twice, one copy for Claude and one for other AI tools.
- **DESIGN.md and the DESIGN-*.md set** - the UI standard, plus one design record per subsystem:
  recording concurrency, channel search, secret handling, live vs. VOD classification, and so on.
- **dev/docs/BUGS.md** - an append-only log of every defect I have fixed, each with its symptom,
  root cause, fix, and the invariant a regression test should assert. Much of the test suite was
  written straight out of it.
- **dev/changelog/NNN** - one numbered file per piece of work: what was asked for, what was
  considered, what was decided, and what actually shipped.
- **dev/docs/ARCHITECTURE.md** - the database schema, the recording state machine, the event
  catalog, and the full config reference.

## Why they are not published

They are my internal design record, written while the work is happening. They are not cleaned up for
an outside reader. They carry decisions that were later reversed and left in place on purpose,
half-formed ideas I have not come back to, and a fair amount of thinking out loud.

Keeping them honest is worth more to me than making them presentable, so they are not published. The
alternative was to strip fifteen hundred references out of the code, which would have cost me the
explanations attached to them, and I would rather tell you what the references are.

## You do not need them

This is the part that matters. A citation is a provenance note, not a required lookup.

Every comment here is written to stand on its own. The reference on the end tells you where a rule
is enforced and why it exists; it is not somewhere you have to go before the line above it makes
sense. If you hit a comment that only works once you have opened a file you do not have, that is a
defect in the comment, and I would like to hear about it.

## Two conventions worth knowing

**A test docstring beginning `Guards dev/docs/BUGS.md <date>`** means that test exists because of one
specific defect that really happened on that date. It is not generic coverage. If you change
something and one of those goes red, you have most likely reintroduced the original bug, and the
docstring describes it.

**A comment ending in a bare `(dev/changelog/430)`** points at the write-up of the change that
introduced the constraint. Those numbers are assigned in order and never reused, so the reference
stays stable even though you cannot read it from here. Two comments citing the same number came out
of the same piece of work.
