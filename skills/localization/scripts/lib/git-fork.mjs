// Git fork-point resolution for branch-scoped discovery and validation.
// Vendored into the skill so the scripts are self-contained: the workflow never
// fetches or contacts the remote — branch scope reflects the developer clone's
// locally cached remote-tracking refs only.

import { spawnSync } from "node:child_process";

// Run a command and return its trimmed stdout. A non-zero exit throws with the
// command line and stderr; pass { required: false } to get "" instead — used to
// probe for refs that may not exist.
function run(command, arguments_, { required = true, cwd } = {}) {
  const result = spawnSync(command, arguments_, { encoding: "utf8", cwd });
  if (result.status === 0) {
    return (result.stdout || "").trim();
  }
  if (!required) {
    return "";
  }
  const stderr = (result.stderr || "").trim();
  const error = stderr || result.error?.message || `exit status ${result.status}`;
  throw new Error(`${[command, ...arguments_].join(" ")} failed: ${error}`);
}

function gitProbe({ cwd, git } = {}) {
  const command =
    git ??
    ((arguments_) => {
      return run("git", arguments_, { cwd, required: false });
    });
  return (arguments_) => {
    const value = command(arguments_);
    return typeof value === "string" && value.trim() ? value.trim() : null;
  };
}

function remoteBranchName(reference) {
  return reference?.replace(/^refs\/remotes\//, "").replace(/^origin\//, "") ?? "";
}

// Resolve the locally cached remote-tracking ref that best represents origin's
// default branch. This intentionally never fetches: branch scope must reflect
// the developer clone's current local memory of the remote.
//
// Artifact worktrees can carry an origin/HEAD symref that points at the checked-out
// feature branch. Trusting that self-reference collapses a merge-base to HEAD and
// silently produces an empty scope, so prefer a single cached origin/main or
// origin/master ref in that case. Ambiguous or untrustworthy state fails closed.
//
// `git`, when supplied, receives an argv array and returns stdout or a falsy value.
// The injection keeps this resolver reusable by callers with their own subprocess
// wrappers and makes its decision logic straightforward to test.
export function resolveDefaultRemoteRef({ cwd = process.cwd(), git } = {}) {
  const command = gitProbe({ cwd, git });
  const symbolic =
    command(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"]) ??
    command(["symbolic-ref", "refs/remotes/origin/HEAD"])?.replace(/^refs\/remotes\//, "");
  const conventional = ["origin/main", "origin/master"].filter(
    (reference) => command(["rev-parse", "--verify", "--quiet", reference]) !== null,
  );
  const current = command(["branch", "--show-current"]) ?? "";
  const headCommit = command(["rev-parse", "HEAD"]);
  const symbolicCommit = symbolic ? command(["rev-parse", symbolic]) : null;
  const symbolicName = remoteBranchName(symbolic);
  const isSymbolicIsConventional = symbolicName === "main" || symbolicName === "master";
  const isSymbolicIsCurrent =
    Boolean(symbolic) &&
    !isSymbolicIsConventional &&
    (symbolicName === current || (headCommit !== null && symbolicCommit === headCommit));

  if (isSymbolicIsCurrent && conventional.length === 0) {
    throw new Error(
      `cannot trust origin/HEAD -> ${symbolic} because it points at the current feature branch`,
    );
  }
  let reference = symbolic;
  let warning = null;
  if ((!reference || isSymbolicIsCurrent) && conventional.length > 0) {
    if (conventional.length > 1) {
      throw new Error(`cannot choose between ${conventional.join(" and ")}`);
    }
    reference = conventional[0];
    if (isSymbolicIsCurrent) {
      warning = `ignored suspicious origin/HEAD -> ${symbolic}; using ${reference}`;
    }
  }
  if (!reference) {
    throw new Error("cannot resolve the default remote branch");
  }
  return { ref: reference, warning };
}

// Resolve origin's cached default branch and the immutable commit where HEAD
// forked from it. Callers may append their own guidance to resolver errors.
export function resolveBranchForkPoint(root = process.cwd(), { git } = {}) {
  const command = gitProbe({ cwd: root, git });
  const { ref, warning } = resolveDefaultRemoteRef({ cwd: root, git: command });
  const forkPoint = command(["merge-base", "HEAD", ref]);
  if (forkPoint === null) {
    throw new Error(`cannot compute the merge-base of HEAD and ${ref}`);
  }
  return { forkPoint, remoteRef: ref, warning };
}
