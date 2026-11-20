#!/usr/bin/env bash
# Prints the title an issue should carry: its kind in square brackets, lowercase, then the
# reporter's own words. The kind comes from the label the issue form applied, never from what
# the reporter typed, so a deleted or misspelled prefix is put back.
#
# Reads ISSUE_TITLE and ISSUE_LABELS (comma-separated) from the environment. An issue title
# is text a stranger controls, so it is never passed on a command line or evaluated.
# An issue with none of the labels below keeps its title exactly.
set -euo pipefail

title="${ISSUE_TITLE-}"
labels=",${ISSUE_LABELS-},"

# label:prefix, first match wins. A new kind is one line here plus its form's `title:`.
KINDS=(
    "bug:bug"
    "enhancement:feature"
)

prefix=""
known=""
for kind in "${KINDS[@]}"; do
    label="${kind%%:*}"
    word="${kind##*:}"
    known="${known:+$known|}$word"
    if [[ -z "$prefix" && "$labels" == *",$label,"* ]]; then
        prefix="$word"
    fi
done

if [[ -z "$prefix" ]]; then
    printf '%s\n' "$title"
    exit 0
fi

# Drop a prefix that is already there in any spelling ("[Bug]", "[BUG] :", "[feature]:").
shopt -s nocasematch
if [[ "$title" =~ ^[[:space:]]*\[($known)\][[:space:]]*:?[[:space:]]*(.*)$ ]]; then
    title="${BASH_REMATCH[2]}"
fi
shopt -u nocasematch

printf '[%s]: %s\n' "$prefix" "$title"
