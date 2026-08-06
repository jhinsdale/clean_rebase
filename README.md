# If You Can Merge It, You Can Rebase It!

[![Clean rebase history diagram](https://raw.githubusercontent.com/jhinsdale/clean_rebase/main/clean_rebase.png)](https://github.com/jhinsdale/clean_rebase/blob/main/clean_rebase.png)

`clean_rebase.py` turns the result of a clean merge into a clean, linear
rebase.

This is useful for a long-lived feature branch that has periodically merged
its target branch to stay current. Such merges make daily development
practical and help keep the feature compatible with ongoing development. But
they also leave intertwined history behind. Before publishing the feature
branch, this tool replays its work directly on top of the target branch and
removes those merge commits.

The tool proceeds only when the feature can merge cleanly into the target. It
uses that successful merge as the source of truth, then verifies that the
rebased branch produces exactly the same final tree. If replay alone leaves
differences, it adds one final reconciliation commit to match the merge.

In short:

```text
Before:                         After:

       A---B---C  target              A---B---C  target
      /     \                              \
 F1--F2------M---F3  feature                F1'--F2'--F3'  feature-rebased
```

The new branch is fully additive on top of `target`: it contains no merge
commits in the rebased range, but its files match the clean merge result.

## Requirements

- Python 3
- Git with worktree and rebase support
- A clean index and working tree for tracked files

No third-party Python packages are required. Untracked files are ignored.

## Usage

Run the script from anywhere inside the Git repository:

```bash
./clean_rebase.py SOURCE TARGET
```

For example, to linearize `feature/search` on top of `main`:

```bash
./clean_rebase.py feature/search main
```

By default, the result is written to `SOURCE-rebased`. The command above
creates `feature/search-rebased`. Your current checkout and original source
branch are not changed.

Inspect the result:

```bash
git log --graph --oneline main..feature/search-rebased
git diff feature/search feature/search-rebased
```

When satisfied, adopt it while the source branch is checked out:

```bash
git reset --hard feature/search-rebased
git branch -D feature/search-rebased
```

The script prints these exact adoption commands after a successful run. It
does not rewrite the source branch automatically.

### Options

```text
-b, --new-branch NAME  Choose the output branch name
-f, --force            Replace an existing output branch and skip target-
                       freshness prompts
-y, --yes              Accept each prompt's default without weakening checks
-v, --verbose          Print every Git command
-h, --help             Show command help
```

Use `-h -v` together to print the full algorithm description:

```bash
./clean_rebase.py -h -v
```

`TARGET` may be a local branch, a remote-tracking branch such as
`origin/main`, a tag, a commit SHA, or any other ref that resolves to a
commit. `SOURCE` must be a local branch.

### Automated use

Use `--yes` for a non-interactive run that retains normal safety behavior:

```bash
./clean_rebase.py --yes feature/search origin/main
```

`--force` has broader meaning: it also overwrites the output branch and skips
the target-upstream freshness check. Use it only when those effects are
intended.

## How it works

### 1. Preflight checks

The script verifies that:

- it is running in a Git repository;
- tracked changes are neither staged nor unstaged;
- no merge, rebase, cherry-pick, revert, or bisect is already in progress;
- `SOURCE` is a local branch and `TARGET` resolves to a commit;
- the output branch is safe to create or replace; and
- there is work to linearize.

When possible, it also offers to fetch the target's upstream and fast-forward
a stale local target. It warns when the source differs from its remote
counterpart. These checks help catch a correct operation performed against
the wrong or stale refs.

### 2. Establish the ground truth

The tool creates a temporary, detached Git worktree at `TARGET` and runs:

```bash
git merge --no-ff --no-commit SOURCE
```

If this merge conflicts, the tool stops. That is the boundary expressed by
the project title: the method applies only when Git can first produce a clean
merge.

If the merge succeeds, the tool commits it in the temporary worktree and
records its tree. A temporary branch named `clean-rebase-scratch-<pid>` keeps
that merge reachable during the operation.

### 3. Replay the source

The output branch begins at `SOURCE`. Inside the temporary worktree, the tool
runs:

```bash
git rebase TARGET
```

Git replays the source's unique commits on top of the target. Merge commits
disappear, and commits already satisfied by the target may become empty and
be dropped.

### 4. Resolve rebase conflicts from the merge tree

A merge can be clean even when replaying its commits one at a time causes
conflicts. When the rebase stops, the tool resolves each conflicted path to
the version found in the previously proven merge tree:

- if the path exists in the merge result, that version is checked out and
  staged;
- if the path is absent from the merge result, the path is removed; and
- if a replayed commit becomes empty, it is skipped.

The loop detects a repeated no-progress state and has a 200-iteration safety
limit. Original commit authors, dates, and messages are retained by Git, but
an individual replayed commit may contain the final merged version of a
conflicted file rather than that commit's original version.

### 5. Reconcile remaining differences

A replay can differ from the clean merge even when Git reports no conflict
for the affected paths. For example, a source commit may add a file already
present in the target, then a later commit deletes it. During replay, Git can
drop the redundant addition and apply the deletion cleanly, even though the
clean merge keeps the file.

After replay, the tool compares the output branch with the recorded merge
tree. If they differ, it restores that exact tree in the temporary worktree
and creates one final, non-merge commit:

```bash
git read-tree --reset -u MERGE_TREE
git commit --no-verify -m "clean_rebase: reconcile ..."
```

The commit message lists the affected paths and their change statuses. The
run summary reports this reconciliation commit and its path count separately
from the number of source commits replayed. If the trees already match, no
extra commit is added.

### 6. Verify the result

Success requires both content and history checks:

```bash
git diff --quiet NEW_BRANCH clean-rebase-scratch-<pid>
git merge-base --is-ancestor TARGET NEW_BRANCH
git rev-list --merges TARGET..NEW_BRANCH
```

The final tree must exactly match the clean merge, `TARGET` must be an
ancestor of the new branch, and the rebased range must contain no merge
commits. Only then is the scratch branch deleted and the result reported as
successful.

If the final tree comparison still fails, the tool reports an error and keeps
both the output branch and scratch merge branch for inspection with the
printed `git diff` command. An output branch still pointing directly at
`SOURCE` is always removed because it is not a valid rebase result.

## Bulletproofing

Rewriting history deserves strong guardrails. The script protects the
repository and makes failures inspectable in several ways.

- **The caller's checkout stays untouched.** All merge and rebase work happens
  in a temporary Git worktree. The caller's working tree, index, current
  branch, and source branch do not move. A successful run creates the output
  branch; an accepted fetch or target fast-forward may also update refs.
- **Dirty tracked files stop the run.** Both staged and unstaged changes are
  rejected before any work starts. Untracked files are ignored because the
  isolated worktree cannot overwrite them.
- **Concurrent Git operations stop the run.** The script detects an active
  merge, rebase, cherry-pick, revert, or bisect and asks the operator to finish
  it first.
- **Refs are checked carefully.** `SOURCE` must be a local branch. `TARGET`
  must resolve to a commit. The output branch must differ from both. If an
  output branch already exists, the script describes whether it resembles a
  valid result or debris from a failed run before asking to remove it.
- **Remote staleness is visible.** The script warns when the source differs
  from its upstream. For a target with an upstream, it offers to fetch and,
  when safe, fast-forward a stale local target. Declined checks and divergent
  targets produce prominent warnings. `--force` explicitly skips this
  hygiene; `--yes` does not.
- **A clean merge is mandatory.** The dry-run merge happens before any rebase.
  If it conflicts, the script lists the conflicted paths, aborts the scratch
  merge, and produces no result.
- **The expected tree stays reachable.** The clean merge is pinned to a real
  scratch branch for the full run. If verification fails, that branch remains
  available for `git diff` and diagnosis.
- **Conflict resolution cannot spin forever.** The script tracks both the
  stopped commit and its unmerged paths. Repeating the same state is treated
  as no progress. The resolution loop also stops after 200 iterations.
- **A stale Git stat cache gets one safe retry.** Some network filesystems can
  make a clean worktree briefly look dirty after a file-type change. The tool
  refreshes Git's index metadata and retries the rebase once. If Git still
  refuses, it prints detailed staged, unstaged, submodule, status, and `HEAD`
  diagnostics.
- **Content and history are both verified.** A matching tree alone could hide
  an output branch that was never rebased. The final gate also proves that
  `TARGET` is an ancestor and that no merge commits remain in the rebased
  range. These checks run after any reconciliation commit.
- **Failed output is cleaned up or retained for diagnosis.** An incomplete
  output branch created by the tool is removed if its tree has not passed
  verification, except when the final tree comparison fails: that branch is
  retained for inspection. If the operator approved replacing a pre-existing
  output branch, its old tip is not preserved under that branch name. A failed
  output still pointing directly at `SOURCE` is always removed because it is
  not a valid result.
- **Interrupted runs recover cleanly.** Merge and rebase state in the scratch
  worktree is aborted during cleanup. Temporary directories are removed and
  worktree metadata is pruned. Scratch worktrees left by a killed earlier run
  are detected and removed on the next invocation.
- **Useful failure evidence survives.** When the final trees differ or another
  meaningful failure occurs, the scratch merge branch is retained and named
  in the error output. Successful runs delete it.

The tool never pushes or automatically moves the source branch to the rebased
result. It fetches only when confirmed interactively or when `--yes` accepts
the default answer.
