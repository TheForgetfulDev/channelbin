#!/usr/bin/env bash
# Prints the address of a pull request run of tests.yml that already passed over the exact tree
# a push to main just landed, or nothing, in which case the suite runs on the push.
#
# Every change reaches main through a pull request, and that pull request's run is the check
# branch protection requires. A pull request run tests the pull request merged into main as main
# stood at the time, so when the merged pull request's own tree is the tree now on main, that is
# the tree the run passed, and running the suite over it again tests nothing new. When another
# pull request landed in between, the trees differ and the suite runs.
#
# Reads GITHUB_REPOSITORY and GITHUB_SHA, and GH_TOKEN for gh. A question GitHub does not answer
# prints nothing, so the suite runs: a lookup that failed is never read as "already tested".
set -uo pipefail

WORKFLOW=tests.yml
repo="${GITHUB_REPOSITORY:?}"
sha="${GITHUB_SHA:?}"

tree_of() {
    gh api "repos/$repo/git/commits/$1" --jq .tree.sha
}

if ! tree="$(tree_of "$sha")" || [[ -z "$tree" ]]; then
    echo "already-tested: could not read the tree of $sha, so the suite runs" >&2
    exit 0
fi

if ! heads="$(gh api "repos/$repo/commits/$sha/pulls" \
                --jq '.[] | select(.merged_at != null) | .head.sha')"; then
    echo "already-tested: could not list the pull requests behind $sha, so the suite runs" >&2
    exit 0
fi

for head in $heads; do
    head_tree="$(tree_of "$head")" || continue
    [[ "$head_tree" == "$tree" ]] || continue
    url="$(gh api "repos/$repo/actions/workflows/$WORKFLOW/runs?head_sha=$head&event=pull_request&status=success" \
             --jq '.workflow_runs[0].html_url // empty')" || continue
    if [[ -n "$url" ]]; then
        printf '%s\n' "$url"
        exit 0
    fi
done
