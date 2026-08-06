#!/usr/bin/env python3
# pylint: skip-file
"""clean_rebase.py -- linearize a clean merge into a rebase.

Given a local branch SOURCE and any ref TARGET where `git merge SOURCE` into
TARGET succeeds cleanly, produce a new branch (default ${SOURCE}-rebased)
whose:
  - history = TARGET + SOURCE's unique commits replayed on top (no merge
    commit), and
  - tip tree = exactly the tree of that successful merge (a final
    reconciliation commit is added when the replay alone falls short).

Refuses to run when the merge would conflict.

Run `clean_rebase.py -h -v` for the detailed algorithm.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile


class Fail(Exception):
    pass


VERBOSE = False
ASSUME_YES = False


def log(msg):
    print(msg, file=sys.stderr)


def log_partial(msg):
    """Write to stderr without newline; flush so it appears before the next
    (potentially slow) operation."""
    sys.stderr.write(msg)
    sys.stderr.flush()


def vlog(msg):
    if VERBOSE:
        log(msg)


def git(*args, cwd=None, check=True, capture=True, stdin=None):
    """Run git with args. Return CompletedProcess.

    check=True raises on non-zero. capture=True captures stdout+stderr as text.
    """
    cmd = ["git"] + list(args)
    vlog("+ " + " ".join(cmd) + (f"  (cwd={cwd})" if cwd else ""))
    kwargs = {"cwd": cwd, "text": True}
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    if stdin is not None:
        kwargs["input"] = stdin
    proc = subprocess.run(cmd, **kwargs)
    if check and proc.returncode != 0:
        out = (proc.stdout or "") + (proc.stderr or "")
        raise Fail(
            "git {} failed (exit {}):\n{}".format(
                " ".join(args), proc.returncode, out.rstrip()
            )
        )
    return proc


def git_out(*args, cwd=None):
    return git(*args, cwd=cwd).stdout.strip()


def rev_parse(ref, cwd=None):
    proc = git("rev-parse", "--verify", "--quiet", ref + "^{commit}",
               cwd=cwd, check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def is_ancestor(a, b, cwd=None):
    proc = git("merge-base", "--is-ancestor", a, b, cwd=cwd, check=False)
    return proc.returncode == 0


def confirm(prompt, force, default=False):
    if force:
        return False
    if ASSUME_YES:
        vlog("assuming the default ({}) for: {}".format(
            "yes" if default else "no", prompt))
        return default
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        ans = input(prompt + suffix).strip().lower()
    except EOFError:
        return default
    if ans == "":
        return default
    return ans in ("y", "yes")


def refresh_stat_cache(cwd=None):
    """Re-stat tracked files so a stale stat cache cannot masquerade as
    unstaged changes.

    Git's clean-worktree check compares cached stat data against the worktree.
    On a network filesystem, or after another git process has rewritten the
    index, entries can look modified when their content is identical, and
    `git rebase` then refuses to start with "cannot rebase: You have unstaged
    changes" while `git status` afterwards reports a clean tree. Refreshing
    first makes that class of false positive impossible.

    Returns the lines git emitted (paths it could not refresh); normally [].
    """
    proc = git("update-index", "-q", "--refresh", cwd=cwd, check=False)
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return [ln for ln in out.splitlines() if ln.strip()]


def reap_stale_scratch_worktrees():
    """Remove leftover scratch worktrees from earlier runs of this tool.

    A run that is killed leaves its clean-rebase-wt-* worktree registered,
    sometimes with a rebase still in progress and NEW_BRANCH checked out in it.
    That blocks the next run from deleting or reusing that branch, with an
    error naming a directory the operator has never heard of. Only paths this
    tool creates are touched; the caller's own worktrees are left alone.
    """
    removed = []
    proc = git("worktree", "list", "--porcelain", check=False)
    for line in (proc.stdout or "").splitlines():
        if not line.startswith("worktree "):
            continue
        path = line[len("worktree "):].strip()
        if not os.path.basename(path).startswith("clean-rebase-wt-"):
            continue
        git("worktree", "remove", "--force", path, check=False)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        removed.append(path)
    if removed:
        git("worktree", "prune", check=False)
    return removed


def clone_anomaly_warnings():
    """Describe clone-level oddities that make git refuse operations for
    reasons `git status` never shows.

    The motivating case: a submodule removed from the tree long ago, whose
    `submodule.<path>.url` entry survives in .git/config, sometimes with an
    embedded .git directory still sitting in the worktree. Such a clone
    behaves normally until an operation consults submodule config.
    """
    warnings = []
    proc = git("config", "--get-regexp", r"^submodule\..*\.url", check=False)
    for line in (proc.stdout or "").splitlines():
        parts = line.split(None, 1)
        if not parts:
            continue
        key = parts[0]
        path = key[len("submodule."):-len(".url")]
        in_head = (git("ls-tree", "HEAD", "--", path,
                       check=False).stdout or "")
        if "160000" not in in_head:
            warnings.append(
                "stale submodule config for '{}': .git/config still "
                "configures it, but HEAD has no gitlink there (leftover from "
                "a removed submodule)".format(path))
        if path and os.path.exists(os.path.join(path, ".git")):
            warnings.append(
                "embedded git repository at '{}' inside the worktree".format(
                    path))
    return warnings


def classify_existing_branch(name, source, target):
    """Describe an existing NEW_BRANCH so the operator can tell a real result
    from wreckage left by a failed run.

    A leftover created-but-never-rebased branch still points at SOURCE, and it
    is a trap: comparing its tree against SOURCE succeeds trivially, so the
    obvious sanity check passes on a branch that was never rebased at all.
    """
    sha = rev_parse(name)
    if sha is None:
        return None
    if sha == rev_parse(source):
        return ("points at SOURCE ({}) -- NOT a rebase result; it is the "
                "un-rebased copy a failed run left behind".format(source))
    merges = git_out("rev-list", "--count", "--merges",
                     "{}..{}".format(target, name))
    if not is_ancestor(target, name):
        return "is not on top of {} -- not a usable result".format(target)
    if merges != "0":
        return ("still contains {} merge commit(s) -- not linear, so not a "
                "finished result".format(merges))
    count = git_out("rev-list", "--count", "{}..{}".format(target, name))
    return ("looks like a completed run: {} commits on {}, no merges"
            .format(count, target))


def source_staleness(source):
    """Return a warning if SOURCE differs from its remote counterpart.

    Guards against the easiest catastrophic mistake: running in the wrong
    clone. A stale SOURCE rebases perfectly happily and reports success.
    """
    up = get_upstream(source)
    up_ref = up[2] if up else None
    if up_ref is None:
        for remote in (git("remote", check=False).stdout or "").split():
            cand = "refs/remotes/{}/{}".format(remote, source)
            if rev_parse(cand) is not None:
                up_ref = cand
                break
    if up_ref is None:
        return None
    local_sha, up_sha = rev_parse(source), rev_parse(up_ref)
    if not local_sha or not up_sha or local_sha == up_sha:
        return None
    short = up_ref.replace("refs/remotes/", "")
    behind = git_out("rev-list", "--count", "{}..{}".format(source, up_ref))
    ahead = git_out("rev-list", "--count", "{}..{}".format(up_ref, source))
    return ("SOURCE '{}' ({}) differs from {} ({}): {} behind, {} ahead. "
            "If this is not the clone you meant to rebase, stop now."
            .format(source, local_sha[:12], short, up_sha[:12], behind, ahead))


def index_and_worktree_clean(cwd=None):
    """Return (ok, reason). ok=False if index has staged changes or worktree
    has unstaged modifications to tracked files. Untracked files are ignored."""
    staged = git("diff", "--cached", "--quiet", cwd=cwd, check=False,
                 capture=False)
    if staged.returncode != 0:
        return False, "index has staged changes"
    unstaged = git("diff", "--quiet", cwd=cwd, check=False, capture=False)
    if unstaged.returncode != 0:
        return False, "worktree has unstaged modifications to tracked files"
    return True, None


def any_op_in_progress(git_dir):
    for marker in ("rebase-merge", "rebase-apply", "MERGE_HEAD",
                   "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG"):
        if os.path.exists(os.path.join(git_dir, marker)):
            return marker
    return None


def branch_exists(name):
    return rev_parse("refs/heads/" + name) is not None


def current_branch():
    proc = git("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def get_upstream(branch):
    """Return (remote, remote_branch, full_ref) for branch@{upstream} or None."""
    proc = git("rev-parse", "--abbrev-ref", "--symbolic-full-name",
               branch + "@{upstream}", check=False)
    if proc.returncode != 0:
        return None
    upstream_short = proc.stdout.strip()
    if "/" not in upstream_short:
        return None
    remote, rbranch = upstream_short.split("/", 1)
    full = "refs/remotes/" + upstream_short
    if rev_parse(full) is None:
        return None
    return remote, rbranch, full


def classify_target(target):
    """Return (kind, remote_name).

    kind == 'local'  -> refs/heads/<target> exists (local branch).
    kind == 'remote' -> refs/remotes/<target> exists (remote-tracking ref).
    kind == 'ref'    -> resolves as a commit some other way (tag, SHA, ...).
    kind == None     -> does not resolve at all.
    """
    if rev_parse("refs/heads/" + target) is not None:
        return "local", None
    if rev_parse("refs/remotes/" + target) is not None:
        remote = target.split("/", 1)[0]
        return "remote", remote
    if rev_parse(target) is not None:
        return "ref", None
    return None, None


def hygiene_check_target(target, kind, remote_name, force):
    """Preflight: freshen TARGET before trusting it.

    Returns a list of warning strings to emit LOUDLY later (only if the
    operation turns out to be non-empty). The list is empty when freshness
    was verified successfully or there is genuinely nothing to check.

    kind == 'local': if TARGET has an @{upstream}, prompt to fetch, then
        prompt to fast-forward the local branch.
    kind == 'remote': TARGET *is* a remote-tracking ref (e.g. origin/main).
        Prompt to fetch it so the remote-tracking ref is current.
    """
    warnings = []
    if force:
        vlog("--force set, skipping TARGET freshness check")
        return warnings

    if kind == "remote":
        rbranch = target.split("/", 1)[1]
        if not confirm(
            "TARGET ('{}') is a remote-tracking ref. "
            "Fetch {} {} to freshen it?".format(target, remote_name, rbranch),
            force=False, default=True,
        ):
            warnings.append(
                "Fetch of {} {} was DECLINED; TARGET '{}' freshness "
                "against the remote is NOT verified.".format(
                    remote_name, rbranch, target))
            return warnings
        log_partial("Verifying by fetching {} {} ...".format(
            remote_name, rbranch))
        try:
            git("fetch", remote_name, rbranch)
        except Fail:
            sys.stderr.write("\n")
            raise
        log(" OK: {} is now current with the remote".format(target))
        return warnings

    # kind == 'local'
    up = get_upstream(target)
    if up is None:
        vlog("TARGET has no configured upstream; skipping freshness check")
        return warnings

    remote, rbranch, full = up
    if not confirm(
        "TARGET ('{}') tracks {}/{}. Fetch that remote now?".format(
            target, remote, rbranch),
        force=False, default=True,
    ):
        warnings.append(
            "Fetch of {}/{} was DECLINED; local TARGET '{}' freshness "
            "against the remote is NOT verified.".format(
                remote, rbranch, target))
        return warnings

    log_partial("Verifying by fetching {} {} ...".format(remote, rbranch))
    try:
        git("fetch", remote, rbranch)
    except Fail:
        sys.stderr.write("\n")
        raise

    local_sha = rev_parse("refs/heads/" + target)
    remote_sha = rev_parse(full)
    if local_sha == remote_sha:
        log(" OK: TARGET is up to date with {}/{}".format(remote, rbranch))
        return warnings
    if is_ancestor(local_sha, remote_sha):
        n = git_out("rev-list", "--count", "{}..{}".format(local_sha, remote_sha))
        log(" OK: TARGET is behind {}/{} by {} commit(s).".format(
            remote, rbranch, n))
        if confirm("Fast-forward TARGET to {}/{}?".format(remote, rbranch),
                   force=False, default=True):
            if current_branch() == target:
                raise Fail(
                    "TARGET '{}' is the currently checked-out branch; "
                    "cannot fast-forward it in place. Check out another "
                    "branch and re-run.".format(target))
            git("update-ref", "refs/heads/" + target, remote_sha, local_sha)
            log("TARGET updated {} -> {}".format(
                local_sha[:12], remote_sha[:12]))
        else:
            warnings.append(
                "Fast-forward of TARGET '{}' to {}/{} was DECLINED; "
                "local TARGET is STALE (behind remote by {} commit(s)). "
                "The rebase result will be relative to the local tip, "
                "NOT the remote.".format(target, remote, rbranch, n))
        return warnings
    if is_ancestor(remote_sha, local_sha):
        log(" OK: TARGET is ahead of upstream; nothing to fetch-integrate")
        return warnings
    log(" WARN: TARGET has DIVERGED from {}/{}".format(remote, rbranch))
    warnings.append(
        "TARGET '{}' has DIVERGED from {}/{}. Continuing with the local "
        "ref; the rebase basis is NOT identical to the remote.".format(
            target, remote, rbranch))
    return warnings


def emit_loud_warnings(warnings):
    if not warnings:
        return
    bar = "!" * 72
    log("")
    log(bar)
    log("!!! WARNING: PROCEEDING WITH UNVERIFIED / STALE TARGET".ljust(72, " "))
    log(bar)
    for w in warnings:
        log("!!! " + w)
    log(bar)
    log("")


def enumerate_unmerged(cwd=None):
    """Return list of unmerged paths using ls-files -u."""
    proc = git("ls-files", "-u", "-z", cwd=cwd)
    if not proc.stdout:
        return []
    paths = set()
    for entry in proc.stdout.split("\0"):
        if not entry:
            continue
        # format: "<mode> <sha> <stage>\t<path>"
        try:
            _meta, path = entry.split("\t", 1)
        except ValueError:
            continue
        paths.add(path)
    return sorted(paths)


def rebase_in_progress(git_dir):
    return (os.path.exists(os.path.join(git_dir, "rebase-merge")) or
            os.path.exists(os.path.join(git_dir, "rebase-apply")))


def rebase_position(git_dir):
    """Return (current, total) commit position of the running rebase, or None.

    git tracks this in the rebase state directory; without it the conflict
    loop is silent for as long as the rebase takes, which on a large branch
    looks indistinguishable from a hang.
    """
    vals = []
    for name in ("msgnum", "end"):
        try:
            with open(os.path.join(git_dir, "rebase-merge", name)) as fh:
                vals.append(fh.read().strip())
        except OSError:
            return None
    if len(vals) != 2 or not all(vals):
        return None
    return vals[0], vals[1]


def progress(msg):
    """Report progress: rewritten in place on a terminal, one line per update
    when redirected to a file or a pipe."""
    if sys.stderr.isatty():
        sys.stderr.write("\r  " + msg.ljust(72))
        sys.stderr.flush()
    else:
        log("  " + msg)


def end_progress():
    if sys.stderr.isatty():
        sys.stderr.write("\n")
        sys.stderr.flush()


def stopped_commit(git_dir, cwd=None):
    """Return the SHA the rebase is currently stopped on, or None.

    This is what distinguishes "the same commit conflicted twice, so we are
    stuck" from "the next commit conflicts on the same file, which is normal
    and progress is being made".

    git_dir is the git dir of the worktree running the rebase -- for a linked
    worktree that is .git/worktrees/<id>, not the repository's .git.
    """
    proc = git("rev-parse", "--quiet", "--verify", "REBASE_HEAD", cwd=cwd,
               check=False)
    if proc.returncode == 0 and (proc.stdout or "").strip():
        return proc.stdout.strip()
    for backend in ("rebase-merge", "rebase-apply"):
        path = os.path.join(git_dir, backend, "stopped-sha")
        try:
            with open(path) as fh:
                return fh.read().strip()
        except OSError:
            continue
    return None


def describe_commit(sha):
    """'abc1234 Subject' for error messages; tolerates None."""
    if not sha:
        return "(unknown commit)"
    proc = git("log", "-1", "--format=%h %s", sha, check=False)
    if proc.returncode == 0 and (proc.stdout or "").strip():
        return proc.stdout.strip()
    return sha


def tame_git_noise(text):
    """Strip git's advice block and relabel expected conflict reports.

    While this tool is auto-resolving, a conflict is the normal case, not a
    failure -- but git announces it with 'error:' plus five lines of hints
    telling a human to resolve it by hand. Printed verbatim, a successful run
    reads like a broken one.
    """
    kept = []
    for line in (text or "").splitlines():
        if line.startswith("hint:") or line.startswith("Could not apply"):
            continue
        if line.startswith("error: could not apply"):
            line = "conflict (expected): " + line[len("error: "):]
        kept.append(line)
    return "\n".join(kept).rstrip()


def looks_empty(text):
    """True if git is saying the commit had nothing left to record.

    Matched case-insensitively: git capitalises these ("No changes ...",
    "The previous cherry-pick is now empty ...").
    """
    low = (text or "").lower()
    return ("nothing to commit" in low or
            "no changes" in low or
            "is now empty" in low or
            "empty commit" in low)


def path_exists_in_tree(tree_sha, path):
    proc = git("cat-file", "-e", "{}:{}".format(tree_sha, path), check=False)
    return proc.returncode == 0


# ---------------------------------------------------------------------------
# main flow

ALGORITHM_DETAIL = """\
Algorithm (detailed)
====================

Given SOURCE (must be a local branch) and TARGET (any ref that resolves to a
commit), produce NEW_BRANCH whose history is TARGET + SOURCE's unique commits
replayed on top (no merge commit) and whose tip tree equals the tree of the
successful merge of SOURCE into TARGET.

Preflight
---------
1. Repo is a git repo. Working tree and index are both clean: `git diff
   --cached --quiet` AND `git diff --quiet` both succeed. Untracked files
   are ignored.
2. SOURCE is a local branch (refs/heads/<SOURCE> exists). TARGET resolves
   to a commit; classified as one of:
     - local (refs/heads/<TARGET>)
     - remote-tracking (refs/remotes/<TARGET>)
     - other ref (tag, SHA, ...)
3. Ancestry sanity: exit "nothing to do" if SOURCE == TARGET, if SOURCE is
   an ancestor of TARGET, or if TARGET is an ancestor of SOURCE AND
   TARGET..SOURCE has no merge commits (i.e. SOURCE is already linearly on
   top of TARGET). If TARGET is an ancestor of SOURCE but merge commits
   exist in the range, proceed -- the rebase will linearize them.
4. NEW_BRANCH does not exist (or -f/--force is given).
5. No rebase/merge/cherry-pick/revert/bisect is in progress.
6. TARGET freshness (skipped by -f/--force):
     - local + upstream: prompt to fetch; if behind, prompt to FF locally.
     - local, no upstream: skip.
     - remote-tracking: prompt to fetch the underlying <remote> <branch>.
     - other ref: no associated remote; skip.

Ground truth: the dry-run merge
-------------------------------
Create a scratch worktree checked out at TARGET (detached), then run:

    git merge --no-ff --no-commit SOURCE

If conflicts appear, the operation is not applicable and the script aborts
with the conflicted paths. If clean, the merge is committed inside the
scratch worktree:

    MERGE_TIP  = HEAD in scratch worktree
    MERGE_TREE = HEAD^{tree}

Before removing the worktree, MERGE_TIP is pinned as a real branch
`clean-rebase-scratch-<pid>` so it stays reachable (and inspectable) for
the whole run, and on any failure path.

Linearize
---------
    git branch [-f] NEW_BRANCH SOURCE
    git checkout NEW_BRANCH
    git rebase TARGET

Commits in SOURCE already present in TARGET by patch-id are auto-dropped
by git.

Conflict loop (using merge tree as source of truth)
---------------------------------------------------
While the rebase is paused with unmerged paths (from `git ls-files -u`):

  For each conflicted path P:
    - If P exists in MERGE_TREE:
        git checkout MERGE_TREE -- P
        git add -A -- P
    - Else (P deleted in MERGE_TREE):
        git rm -f -- P

  Then: git rebase --continue.
  If the resulting commit would be empty ("nothing to commit"):
    git rebase --skip.

Reconcile
---------
The conflict loop only sees paths git flags as conflicted. A replay can
still drift from MERGE_TREE without a conflict -- e.g. SOURCE adds a file
TARGET already has, then deletes it: on replay the add is a no-op and the
delete applies cleanly, while the merge (and SOURCE's own merge commits)
kept the file. If the rebased tip tree differs from MERGE_TREE, one final
commit is added that sets the tree to MERGE_TREE, its message listing the
paths:

    git read-tree --reset -u MERGE_TREE
    git commit -m "clean_rebase: reconcile ..."

Loop capped at 200 iterations. If the same set of unmerged paths reappears
with no progress, abort with a diagnostic -- that indicates a genuine
incompatibility the merge tree cannot reconcile.

Why this is correct
-------------------
The dry-run merge established that SOURCE and TARGET have a clean combined
state, represented by MERGE_TREE. Individual commits along the linearized
rebase may end up carrying the final merged version of a file rather than
the version that commit originally added, but the tip tree is verified to
equal MERGE_TREE, history stays linear, and each commit preserves its
original author, date, and message.

Verify + cleanup
----------------
After rebase completes, the final gate is:

    git diff --quiet NEW_BRANCH clean-rebase-scratch-<pid>

If empty, the rebase reproduces the merge exactly. The scratch branch is
then deleted, HEAD is checked out on SOURCE (the operator's original
working branch is left as-is, since post-rebase inspection happens from
SOURCE), and adoption commands are printed:

    git reset --hard NEW_BRANCH
    git branch -D NEW_BRANCH

If non-empty, the script aborts with a diagnostic. The scratch branch and
NEW_BRANCH are both retained so `git diff NEW_BRANCH
clean-rebase-scratch-<pid>` shows the drift. On other failures any partial
NEW_BRANCH the script created is removed; a pre-existing NEW_BRANCH is
never deleted.
"""


def main():
    global VERBOSE, ASSUME_YES

    # This tool replays commits unattended and every replayed commit keeps its
    # original message, so there is never anything for a human to type. Git
    # nevertheless opens core.editor when `rebase --continue` commits a
    # resolved conflict, and that fails outright when stdin is not a tty
    # ("standard input is not a tty" / "there was a problem with the editor"),
    # leaving the rebase paused with nothing unmerged. Tell git not to.
    os.environ["GIT_EDITOR"] = "true"
    os.environ["GIT_SEQUENCE_EDITOR"] = "true"

    ap = argparse.ArgumentParser(
        prog="clean_rebase.py",
        description="Convert a clean merge SOURCE->TARGET into a linear rebase.",
        add_help=False,
    )
    ap.add_argument("-h", "--help", action="store_true",
                    help="show usage and exit; combine with -v/--verbose to "
                         "also print the detailed algorithm description")
    ap.add_argument("source", nargs="?", help="branch/ref to rebase")
    ap.add_argument("target", nargs="?", help="branch/ref to rebase onto")
    ap.add_argument("-b", "--new-branch", default=None,
                    help="output branch (default: ${SOURCE}-rebased)")
    ap.add_argument("-f", "--force", action="store_true",
                    help="overwrite NEW_BRANCH if it exists AND skip all "
                         "interactive hygiene prompts (TARGET upstream check)")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="take the default answer to every prompt and never "
                         "wait for input; unlike --force this changes no "
                         "safety behaviour, it only stops asking")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="log every git invocation; combined with -h also "
                         "prints the detailed algorithm description")
    args = ap.parse_args()

    if args.help:
        ap.print_help()
        if args.verbose:
            print("")
            print(ALGORITHM_DETAIL)
        return 0

    if args.source is None or args.target is None:
        ap.print_usage(sys.stderr)
        print("clean_rebase.py: error: SOURCE and TARGET are required",
              file=sys.stderr)
        return 2

    VERBOSE = args.verbose
    ASSUME_YES = args.yes

    source = args.source
    target = args.target
    new_branch = args.new_branch or (source + "-rebased")

    # sanity: not the same name
    if new_branch == source or new_branch == target:
        log("ERROR: NEW_BRANCH ({}) must differ from SOURCE and TARGET.".format(
            new_branch))
        return 2

    # locate repo
    try:
        toplevel = git_out("rev-parse", "--show-toplevel")
        git_dir = git_out("rev-parse", "--git-dir")
    except Fail as e:
        log("ERROR: not in a git repository:\n{}".format(e))
        return 2
    if not os.path.isabs(git_dir):
        git_dir = os.path.join(toplevel, git_dir)

    # preflight
    try:
        # Record which git actually ran. A conda environment can shadow git on
        # PATH, and git's behaviour around symlink/regular-file type changes
        # differs between versions -- without this, a failed run cannot be
        # reproduced or compared against a successful one.
        vlog("git: {} ({})".format(
            (git("--version", check=False).stdout or "?").strip(),
            (git("--exec-path", check=False).stdout or "?").strip()))

        # 0. defensive: name any clone anomalies up front rather than letting
        #    them surface later as a cryptic refusal from git.
        for path in reap_stale_scratch_worktrees():
            log("Removed a scratch worktree left by an earlier run: {}"
                .format(path))
        for warning in clone_anomaly_warnings():
            log("Warning: {}".format(warning))

        # 1. clean index + working tree (staged AND unstaged)
        ok, reason = index_and_worktree_clean()
        if not ok:
            log("ERROR: {}. Commit, stash, or reset first.".format(reason))
            log("       (Run `git status` to see what's dirty.)")
            return 2

        # 5. no op in progress
        op = any_op_in_progress(git_dir)
        if op:
            log("ERROR: another git operation is in progress ({}). "
                "Resolve it first.".format(op))
            return 2

        # 2. SOURCE must be a local branch; TARGET may be any ref
        if not branch_exists(source):
            log("ERROR: SOURCE '{}' is not a local branch.".format(source))
            return 2
        stale = source_staleness(source)
        if stale:
            log("Warning: " + stale)
        target_kind, target_remote = classify_target(target)
        if target_kind is None:
            log("ERROR: TARGET '{}' does not resolve to any ref.".format(target))
            return 2

        # 6. TARGET freshness hygiene (may prompt; skipped by --force).
        # Applies to local branches with upstream and to remote-tracking refs;
        # for a bare tag/SHA there is no remote to check against. Any
        # declined fetch / declined fast-forward is queued as a loud warning
        # emitted below.
        stale_warnings = hygiene_check_target(
            target, target_kind, target_remote, force=args.force)

        # Emit the loud warnings NOW, before ancestry check. A stale local
        # TARGET can appear as an ancestor of SOURCE and produce a
        # misleading "nothing to do" exit; the operator needs to see the
        # warning either way.
        emit_loud_warnings(stale_warnings)

        # refs resolve (post-hygiene, since hygiene may have moved TARGET)
        source_sha = rev_parse(source)
        target_sha = rev_parse(target)

        # 3. ancestry
        if source_sha == target_sha:
            log("Nothing to do: SOURCE and TARGET point at the same commit.")
            return 0
        if is_ancestor(source_sha, target_sha):
            log("Nothing to do: SOURCE is already an ancestor of TARGET.")
            return 0
        if is_ancestor(target_sha, source_sha):
            # TARGET's tip is reachable from SOURCE, but that alone does NOT
            # mean SOURCE is already linearly rebased onto TARGET -- SOURCE
            # may have merged TARGET in one or more times, leaving merge
            # commits in TARGET..SOURCE that a rebase would still linearize.
            # Only exit "nothing to do" if the range is genuinely linear
            # (no merge commits between TARGET and SOURCE).
            merges = git_out("log", "--merges", "--oneline",
                             "{}..{}".format(target, source))
            if not merges:
                if stale_warnings:
                    log("SOURCE is already linearly rebased onto TARGET "
                        "(TARGET is an ancestor of SOURCE and TARGET..SOURCE "
                        "has no merge commits) FOR THE LOCAL REF ONLY; "
                        "freshness against the remote was NOT verified "
                        "(see warning above). Nothing to do based on the "
                        "local ref.")
                else:
                    log("SOURCE is already linearly rebased onto TARGET "
                        "(TARGET is an ancestor of SOURCE and TARGET..SOURCE "
                        "has no merge commits); nothing to do.")
                return 0
            n_merges = len(merges.splitlines())
            log("Note: TARGET is an ancestor of SOURCE, but TARGET..SOURCE "
                "contains {} merge commit(s); proceeding with the rebase to "
                "linearize.".format(n_merges))

        # 4. NEW_BRANCH availability. If it exists:
        #    -f/--force  -> silently overwrite (existing branch will be
        #                   moved by `git branch -f` later)
        #    otherwise   -> prompt to remove it (default Y). If declined,
        #                   abort.
        if branch_exists(new_branch) and not args.force:
            verdict = classify_existing_branch(new_branch, source, target)
            if verdict:
                log("Existing branch '{}' {}.".format(new_branch, verdict))
            if current_branch() == new_branch:
                log("ERROR: NEW_BRANCH '{}' already exists and is the "
                    "currently checked-out branch. Check out a different "
                    "branch and re-run.".format(new_branch))
                return 2
            if not confirm(
                "NEW_BRANCH '{}' already exists. Remove it and continue?"
                .format(new_branch),
                force=False, default=True,
            ):
                log("Aborted: NEW_BRANCH '{}' exists and removal declined."
                    .format(new_branch))
                return 2
            git("branch", "-D", new_branch)
            log("Removed existing NEW_BRANCH '{}'.".format(new_branch))

    except Fail as e:
        log("ERROR during preflight: {}".format(e))
        return 2

    scratch_branch = "clean-rebase-scratch-{}".format(os.getpid())
    # avoid clobbering a stray leftover
    n = 0
    while branch_exists(scratch_branch):
        n += 1
        scratch_branch = "clean-rebase-scratch-{}-{}".format(os.getpid(), n)

    tmpdir = tempfile.mkdtemp(prefix="clean-rebase-wt-")
    # tempfile.mkdtemp creates the directory, but `git worktree add` needs it
    # not to exist yet. Remove now; worktree add will recreate.
    os.rmdir(tmpdir)

    made_new_branch = False
    worktree_added = False
    scratch_branch_created = False
    diff_ok = False
    keep_new_branch = False
    reconciled = []
    conflicts_resolved = 0
    replayed = 0
    dropped = 0

    def cleanup(success):
        # Remove the scratch worktree if we added it. Everything this tool
        # does happens in there -- the caller's worktree, index and HEAD are
        # never touched -- so aborting an interrupted merge or rebase means
        # aborting it there, and then the whole directory goes away regardless.
        if worktree_added and os.path.exists(tmpdir):
            vlog("cleanup: removing scratch worktree {}".format(tmpdir))
            git("merge", "--abort", cwd=tmpdir, check=False)
            git("rebase", "--abort", cwd=tmpdir, check=False)
            git("worktree", "remove", "--force", tmpdir, check=False)
        # in case worktree remove failed but directory lingers
        if os.path.exists(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)
            git("worktree", "prune", check=False)

        if not success:
            # A NEW_BRANCH still pointing at SOURCE is not a partial result,
            # it is a trap: it passes a tree comparison against SOURCE
            # trivially. Remove it whether or not we created it.
            if (branch_exists(new_branch) and
                    rev_parse(new_branch) == rev_parse(source)):
                log("Removing '{}': it still points at {}, so it is not a "
                    "rebase result.".format(new_branch, source))
                git("branch", "-D", new_branch, check=False)
            # Remove NEW_BRANCH if we created it and didn't finish.
            elif made_new_branch and not diff_ok and not keep_new_branch:
                # Only nuke a branch we made; never one that existed before.
                vlog("cleanup: deleting partial NEW_BRANCH {}".format(new_branch))
                git("branch", "-D", new_branch, check=False)

        # Scratch branch policy: on success + diff_ok, delete it. Otherwise
        # keep it so the operator can inspect.
        if scratch_branch_created:
            if success and diff_ok:
                vlog("cleanup: deleting scratch branch {}".format(scratch_branch))
                git("branch", "-D", scratch_branch, check=False)
            else:
                log("Scratch merge branch retained for inspection: {}".format(
                    scratch_branch))

    try:
        # ---- Step 2: dry-run merge in an isolated worktree ----
        log("Creating scratch worktree at {} on TARGET ({}) ...".format(
            tmpdir, target))
        git("worktree", "add", "--detach", tmpdir, target)
        worktree_added = True

        log("Dry-run merge SOURCE ({}) -> TARGET ({}) in scratch worktree ..."
            .format(source, target))
        proc = git("-C", tmpdir, "merge", "--no-ff", "--no-commit", source,
                   check=False)
        unmerged = enumerate_unmerged(cwd=tmpdir)
        if proc.returncode != 0 or unmerged:
            # not applicable
            log("ERROR: dry-run merge is NOT clean. This operation is not "
                "applicable; resolve the merge upstream first.")
            if unmerged:
                log("Conflicted files:")
                for p in unmerged:
                    log("  " + p)
            git("-C", tmpdir, "merge", "--abort", check=False)
            raise Fail("dry-run merge conflicted")

        # complete the merge
        git("-C", tmpdir, "commit", "--no-edit",
            "-m", "scratch merge {} -> {}".format(source, target))
        merge_tip = git_out("-C", tmpdir, "rev-parse", "HEAD")
        merge_tree = git_out("-C", tmpdir, "rev-parse", "HEAD^{tree}")
        vlog("MERGE_TIP={} MERGE_TREE={}".format(merge_tip, merge_tree))

        # pin merge commit with a real branch before removing the worktree
        git("branch", scratch_branch, merge_tip)
        scratch_branch_created = True
        log("Scratch merge pinned as branch: {} ({})".format(
            scratch_branch, merge_tip[:12]))

        # ---- Step 3: create NEW_BRANCH at SOURCE, checked out in the
        #      scratch worktree ----
        # The rebase runs in the scratch worktree, not the caller's. Two
        # reasons: the caller's worktree, index and HEAD are then never
        # touched -- the only change in their repository is that NEW_BRANCH
        # appears -- and the rebase no longer has to survive a checkout in
        # that worktree, which git can refuse when a path changes type
        # (symlink <-> regular file) on a network filesystem.
        if branch_exists(new_branch):
            # --force path (preflight already gated this)
            git("branch", "-f", new_branch, source)
        else:
            git("branch", new_branch, source)
            made_new_branch = True
        git("checkout", new_branch, cwd=tmpdir)
        wt_git_dir = git_out("rev-parse", "--absolute-git-dir", cwd=tmpdir)
        vlog("rebase runs in {} (git dir {})".format(tmpdir, wt_git_dir))

        # ---- Step 4 + 5: rebase onto TARGET with conflict loop ----
        log("Rebasing {} onto {} (no output until git stops for the first "
            "conflict; this can take a while on a large branch) ...".format(
                new_branch, target))
        proc = git("-c", "advice.mergeConflict=false", "rebase", target,
                   cwd=tmpdir, check=False)
        if (proc.returncode != 0 and rebase_in_progress(wt_git_dir) and
                enumerate_unmerged(cwd=tmpdir)):
            # The normal path: git stopped on a conflict that this tool is
            # about to resolve from the merge tree. Say so, rather than
            # relaying git's error-and-hints block as if something broke.
            log("Conflicts stop the rebase as expected; auto-resolving each "
                "from the merge tree ...")
            vlog(tame_git_noise((proc.stdout or "") + (proc.stderr or "")))
        else:
            if proc.stdout:
                vlog(proc.stdout.rstrip())
            if proc.stderr:
                vlog(proc.stderr.rstrip())

        # If git refused to START the rebase (e.g. "cannot rebase: You have
        # unstaged changes"), do not fall into the conflict loop: it has
        # nothing to resolve and reports a misleading "rebase paused" error.
        # Report git's own message, plus what git considered dirty at the
        # moment it said so -- by the time the run ends, the evidence is gone.
        combined = ((proc.stdout or "") + (proc.stderr or "")).rstrip()
        low = combined.lower()

        # A refusal here is often a false positive from stale stat data rather
        # than real modification. Checking out TARGET can change a path's TYPE
        # (symlink <-> regular file); on a network filesystem the new entry's
        # cached size can lag, so git compares against the old size and calls
        # the path modified. Re-stat and retry once: refreshing re-reads the
        # entry, and a genuinely modified path stays modified.
        if proc.returncode != 0 and ("cannot rebase" in low or
                                     "please commit or stash" in low):
            log("git refused to start the rebase; re-stating the worktree and "
                "retrying once ...")
            if rebase_in_progress(wt_git_dir):
                git("rebase", "--abort", cwd=tmpdir, check=False)
            refresh_stat_cache(cwd=tmpdir)
            still_dirty = (git("diff-files", "--name-only", cwd=tmpdir,
                               check=False).stdout or "").strip()
            if still_dirty:
                log("  still modified after refresh: {}".format(
                    still_dirty.replace("\n", ", ")))
            else:
                vlog("  worktree clean after refresh; retrying rebase")
                git("checkout", new_branch, cwd=tmpdir, check=False)
                proc = git("rebase", target, cwd=tmpdir, check=False)
                combined = ((proc.stdout or "") +
                            (proc.stderr or "")).rstrip()
                low = combined.lower()

        if proc.returncode != 0 and ("cannot rebase" in low or
                                     "please commit or stash" in low):
            log("ERROR: git refused to start the rebase:\n{}".format(combined))
            for label, cmd in (
                    ("unstaged (diff-files)",
                     ("diff-files", "--name-status")),
                    ("unstaged, submodules included",
                     ("diff-files", "--name-status",
                      "--ignore-submodules=none")),
                    ("staged (diff --cached)",
                     ("diff", "--cached", "--name-status")),
                    ("status", ("status", "--porcelain=v2",
                                "--untracked-files=no")),
                    ("HEAD", ("rev-parse", "--abbrev-ref", "HEAD")),
            ):
                out = (git(*cmd, cwd=tmpdir, check=False).stdout or "").strip()
                log("  {:30s} {}".format(
                    label + ":", out.replace("\n", "\n" + " " * 33) or
                    "(clean)"))
            raise Fail("git rebase refused to start; diagnostics above")

        max_iters = 200
        iters = 0
        prev_state = None
        skipped_empty = []
        total_commits = None
        while rebase_in_progress(wt_git_dir):
            iters += 1
            if iters > max_iters:
                raise Fail(
                    "conflict loop exceeded {} iterations; aborting".format(
                        max_iters))
            unmerged = enumerate_unmerged(cwd=tmpdir)
            stopped = stopped_commit(wt_git_dir, cwd=tmpdir)
            if not unmerged:
                # Paused with nothing conflicted. Measured on git 2.47.3: a
                # commit emptied by auto-resolution is dropped silently and
                # --continue returns 0, so this is not the "empty commit"
                # pause it was once assumed to be. Try to continue; if git
                # will not, report it rather than guessing at a --skip.
                proc = git("rebase", "--continue", cwd=tmpdir, check=False)
                if proc.returncode == 0 or not rebase_in_progress(wt_git_dir):
                    continue
                raise Fail(
                    "rebase paused with no unmerged paths and --continue "
                    "failed at {}:\n{}".format(
                        describe_commit(stopped),
                        ((proc.stdout or "") + (proc.stderr or "")).rstrip()))

            # Key the no-progress check on the *commit*, not just the paths:
            # consecutive commits touching one file conflict on the same path
            # set while making perfectly good progress.
            state = (stopped, tuple(unmerged))
            if state == prev_state:
                raise Fail(
                    "commit {} conflicted twice on the same paths with no "
                    "progress; genuine incompatibility that merge tree cannot "
                    "reconcile: {}".format(
                        describe_commit(stopped), ", ".join(unmerged)))
            prev_state = state

            pos = rebase_position(wt_git_dir)
            if pos:
                total_commits = pos[1]
            where = "commit {}/{}".format(*pos) if pos else "commit ?"
            progress("resolving conflicts: {}, {} path(s) resolved so far ..."
                     .format(where, conflicts_resolved))
            vlog("conflict resolution round {}: {} path(s) at {}".format(
                iters, len(unmerged), stopped or "?"))
            for p in unmerged:
                if path_exists_in_tree(merge_tree, p):
                    git("checkout", merge_tree, "--", p, cwd=tmpdir)
                    git("add", "-A", "--", p, cwd=tmpdir)
                else:
                    # not in final tree; remove it from index+worktree
                    git("rm", "-f", "--", p, cwd=tmpdir, check=False)
                conflicts_resolved += 1

            proc = git("rebase", "--continue", cwd=tmpdir, check=False)
            if proc.returncode != 0 and not rebase_in_progress(wt_git_dir):
                # continue failed and rebase no longer in progress -- likely
                # empty commit path. Try --skip if empty.
                # Actually if rebase is no longer in progress, we're done or
                # something went wrong. Break and let post-loop check handle.
                break
            if proc.returncode != 0 and rebase_in_progress(wt_git_dir):
                # if nothing to commit, git says so on stderr; use --skip
                combined = (proc.stdout or "") + (proc.stderr or "")
                if looks_empty(combined):
                    vlog("skipping empty commit {}".format(stopped or "?"))
                    skipped_empty.append(stopped)
                    git("rebase", "--skip", cwd=tmpdir, check=False)

        # Overwrite the last in-place progress line with the finished state:
        # left as-is it reports wherever the final conflict happened to be,
        # which reads as though the run stopped there.
        if conflicts_resolved:
            progress("resolving conflicts: done, {} path(s) resolved "
                     "across {} commits attempted".format(
                         conflicts_resolved, total_commits or "?"))
        end_progress()
        if rebase_in_progress(wt_git_dir):
            raise Fail("rebase still in progress after conflict loop")

        if skipped_empty:
            log("{} commit(s) came out empty after auto-resolution and were "
                "skipped.".format(len(skipped_empty)))
            for sha in skipped_empty:
                vlog("  skipped empty: {}".format(describe_commit(sha)))

        # ---- Step 5b: reconcile drift the conflict loop never saw ----
        # A path can drift from the merge tree without ever conflicting, e.g.
        # SOURCE adds a file TARGET already has and later deletes it: the add
        # replays as a no-op, the delete applies cleanly, but the merge kept
        # the file. Set the tree to the merge tree in one final commit.
        drift = git_out("diff", "--no-renames", "--name-status", new_branch,
                        merge_tree)
        if drift:
            reconciled = drift.splitlines()
            log("Replay drifted from the merge tree on {} path(s) without "
                "conflicting; adding a reconciliation commit:".format(
                    len(reconciled)))
            for line in reconciled:
                log("  " + line)
            git("read-tree", "--reset", "-u", merge_tree, cwd=tmpdir)
            msg = ("clean_rebase: reconcile {} with merge of {} into {}\n\n"
                   "Replaying {} linearly onto {} did not reproduce the merge "
                   "tree\nfor these paths, which never conflicted during the "
                   "rebase:\n\n{}\n".format(
                       new_branch, source, target, source, target,
                       "\n".join("  " + l for l in reconciled)))
            git("commit", "--no-verify", "-m", msg, cwd=tmpdir)

        # ---- Step 6: verify tip tree == merge tree ----
        proc = git("diff", "--quiet", new_branch, scratch_branch, check=False)
        if proc.returncode == 0:
            diff_ok = True
        else:
            # leave the state for the operator; scratch branch retained
            keep_new_branch = True
            log("ERROR: tip tree of {} != merge tree ({}).".format(
                new_branch, scratch_branch))
            log("       Inspect with: git diff {} {}".format(
                new_branch, scratch_branch))
            raise Fail("tip tree mismatch")

        # A tree match alone cannot tell a finished rebase from an untouched
        # copy of SOURCE -- both match. Check the shape as well.
        stray = git_out("rev-list", "--count", "--merges",
                        "{}..{}".format(target, new_branch))
        if stray != "0":
            raise Fail("{} still contains {} merge commit(s); not "
                       "linear".format(new_branch, stray))
        if not is_ancestor(target, new_branch):
            raise Fail("{} is not on top of {}".format(new_branch, target))

        # ---- counts for the summary ----
        # These must reconcile, or the operator is left comparing three
        # numbers that describe the same range and do not add up:
        #   in range = merges linearized + replayed + went empty
        replayed = int(git_out("rev-list", "--count",
                               "{}..{}".format(target, new_branch)))
        if reconciled:
            replayed -= 1
        in_range = nonmerge = merges_dropped = emptied = None
        try:
            base = git_out("merge-base", target, source)
            in_range = int(git_out("rev-list", "--count",
                                   "{}..{}".format(base, source)))
            nonmerge = int(git_out("rev-list", "--count", "--no-merges",
                                   "{}..{}".format(base, source)))
            merges_dropped = in_range - nonmerge
            emptied = nonmerge - replayed
        except Fail:
            pass

        # (No step 7. The caller's HEAD was never moved, so there is nothing
        # to restore: the rebase happened in the scratch worktree.)

        # summary
        new_sha = git_out("rev-parse", new_branch)
        log("")
        log("=== clean_rebase summary ===")
        log("  NEW_BRANCH:              {} ({})".format(
            new_branch, new_sha[:12]))
        if in_range is not None:
            log("  commits in range:        {}  ({} non-merge + {} merge)"
                .format(in_range, nonmerge, merges_dropped))
            log("    merge commits dropped: {}  (linearized)".format(
                merges_dropped))
            log("    replayed:              {}".format(replayed))
            log("    already satisfied:     {}  (became empty on replay)"
                .format(emptied))
        else:
            log("  commits replayed:        {}".format(replayed))
        log("  conflicts auto-resolved: {}".format(conflicts_resolved))
        if reconciled:
            log("  reconciliation commit:   yes, {} path(s) that drifted "
                "without conflicting".format(len(reconciled)))
        log("  git diff {} {}: {}".format(
            new_branch, scratch_branch,
            "empty (OK)" if diff_ok else "NON-EMPTY (FAIL)"))
        log("  scratch branch:          {}".format(
            "deleted (diff empty)" if diff_ok else scratch_branch))
        log("")
        log("To adopt the rebased history onto {}, run:".format(source))
        log("    git reset --hard {}".format(new_branch))
        log("    git branch -D {}".format(new_branch))

        cleanup(success=True)
        return 0

    except Fail as e:
        log("ERROR: {}".format(e))
        cleanup(success=False)
        return 1
    except KeyboardInterrupt:
        log("interrupted")
        cleanup(success=False)
        return 130


if __name__ == "__main__":
    sys.exit(main())
