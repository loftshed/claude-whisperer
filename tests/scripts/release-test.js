import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import {
  bump,
  checkRelease,
  gitIn,
  incrementFor,
  planRelease,
  publishRelease,
  RELEASE_SUBJECT,
  releaseChangelog,
  setFrontmatterVersion,
  setTableVersion,
  writeRelease,
} from "../../scripts/release.js";
import { validate } from "../../scripts/validate-marketplace.js";

describe("version arithmetic", () => {
  test.each([
    [["fix: a"], "patch"],
    [["docs: a", "feat(scope): b"], "minor"],
    [["refactor!: a"], "major"],
    [["fix: a\n\nBREAKING CHANGE: config moved"], "major"],
    [["Plain subject"], "patch"],
  ])("%j asks for a %s release", (messages, increment) => {
    expect(incrementFor(messages)).toBe(increment);
  });

  test.each([
    ["1.2.3", "patch", "1.2.4"],
    ["1.2.3", "minor", "1.3.0"],
    ["1.2.3", "major", "2.0.0"],
    ["0.4.3", "major", "0.5.0"],
  ])("%s plus a %s release is %s", (version, increment, next) => {
    expect(bump(version, increment)).toBe(next);
  });
});

describe("generated text", () => {
  test("Unreleased notes become the release section and history is kept", () => {
    const before =
      "# Changelog\n\n## Unreleased\n\n- Clearer summary.\n\n## 1.0.0 (2026-09-29)\n\n- Old.\n";
    expect(releaseChangelog(before, "1.0.1", ["fix: terse"])).toEqual({
      content: "# Changelog\n\n## 1.0.1\n\n- Clearer summary.\n\n## 1.0.0 (2026-09-29)\n\n- Old.\n",
      notes: "- Clearer summary.",
    });
  });

  test("without notes the commit subjects are listed once each", () => {
    const { content } = releaseChangelog(null, "0.1.0", ["feat: a\n\nbody", "feat: a", "fix: b"]);
    expect(content).toBe("# Changelog\n\n## 0.1.0\n\n- feat: a\n- fix: b\n");
  });

  test("only the named plugin's README row changes", () => {
    const readme = "| `a` | 1.0.0 | x |\n| [`ab`](plugins/ab/README.md) | 1.0.0 | y |\n";
    expect(setTableVersion(readme, "ab", "1.1.0")).toBe(readme.replace("1.0.0 | y", "1.1.0 | y"));
    expect(() => setTableVersion(readme, "zz", "1.0.0")).toThrow("no plugin table row");
  });

  test("only metadata.version in the frontmatter changes", () => {
    const skill = "---\nname: x\nversion: 7\nmetadata:\n  version: 0.1.0\n---\n  version: body\n";
    expect(setFrontmatterVersion(skill, "0.2.0")).toBe(skill.replace("0.1.0", "0.2.0"));
    expect(setFrontmatterVersion("no frontmatter\n", "1.0.0")).toBe("no frontmatter\n");
  });
});

describe("releases in a repository", () => {
  let scratch, root, git, since;
  const file = (relative) => path.join(root, relative);
  const read = (relative) => fs.readFileSync(file(relative), "utf8");
  const json = (relative) => JSON.parse(read(relative));
  const put = (relative, content = `${relative} ${Math.random()}\n`) => {
    fs.mkdirSync(path.dirname(file(relative)), { recursive: true });
    fs.writeFileSync(
      file(relative),
      typeof content === "string" ? content : `${JSON.stringify(content, null, 2)}\n`,
    );
  };
  const commit = (subject, relative, content) => {
    if (relative) put(relative, content);
    git(["add", "--all"]);
    git(["commit", "--quiet", "--allow-empty", "-m", subject]);
    return git(["rev-parse", "HEAD"]);
  };
  const planned = () =>
    planRelease({ root }).releases.map(({ name, version }) => `${name}@${version}`);

  beforeEach(() => {
    scratch = fs.mkdtempSync(path.join(os.tmpdir(), "release-test-"));
    root = path.join(scratch, "repo");
    fs.mkdirSync(root);
    git = gitIn(root);
    git(["init", "--quiet", "--initial-branch=main"]);
    for (const [key, value] of [
      ["user.name", "Test"],
      ["user.email", "test@example.invalid"],
      ["commit.gpgsign", "false"],
      ["core.hooksPath", "/dev/null"],
    ]) {
      git(["config", key, value]);
    }
    put(".claude-plugin/marketplace.json", {
      name: "fixture",
      owner: { name: "tester" },
      plugins: ["one", "two"].map((name) => ({
        description: name,
        name,
        source: `./plugins/${name}`,
      })),
    });
    for (const name of ["one", "two"]) {
      put(`plugins/${name}/.claude-plugin/plugin.json`, { name, version: "0.3.0" });
      put(`plugins/${name}/CHANGELOG.md`, "# Changelog\n\n## 0.3.0\n\n- Earlier.\n");
      put(
        `plugins/${name}/skills/${name}/SKILL.md`,
        `---\nname: ${name}\ndescription: ${name}.\n---\n`,
      );
    }
    put("plugins/two/package.json", { name: "two", version: "0.3.0" });
    put(
      "README.md",
      "| Plugin | Version | About |\n| --- | --- | --- |\n| `one` | 0.3.0 | 1 |\n| [`two`](plugins/two/README.md) | 0.3.0 | 2 |\n",
    );
    since = commit("chore: start");
    commit("chore: count releases from here", "release.json", { since });
  });
  afterEach(() => fs.rmSync(scratch, { force: true, recursive: true }));

  test("each plugin moves by its own commits only", () => {
    commit("feat: new skill", "plugins/one/skills/extra/SKILL.md");
    commit("fix: wording", "plugins/two/skills/two/SKILL.md");
    commit("docs: repository readme", "README.md", `${read("README.md")}\nMore.\n`);
    expect(planned()).toEqual(["one@0.4.0", "two@0.3.1"]);
  });

  test("tests, repository docs, changelogs and reverted edits release nothing", () => {
    commit("test: cases", "tests/one-test.js");
    commit(
      "docs: history",
      "plugins/one/CHANGELOG.md",
      `${read("plugins/one/CHANGELOG.md")}\nNote.\n`,
    );
    const original = read("plugins/two/skills/two/SKILL.md");
    commit("feat: try something", "plugins/two/skills/two/SKILL.md");
    commit("revert: try something", "plugins/two/skills/two/SKILL.md", original);
    expect(planned()).toEqual([]);
  });

  test("deleting a shipped file and editing a marketplace entry both release", () => {
    git(["rm", "--quiet", "plugins/one/skills/one/SKILL.md"]);
    commit("remove a skill");
    const marketplace = json(".claude-plugin/marketplace.json");
    marketplace.plugins[1].description = "Better description";
    commit("fix: describe two", ".claude-plugin/marketplace.json", marketplace);
    expect(planned()).toEqual(["one@0.3.1", "two@0.3.1"]);
  });

  test("a hand-edited version is refused", () => {
    commit("feat: bump", "plugins/one/.claude-plugin/plugin.json", {
      name: "one",
      version: "0.9.0",
    });
    expect(() => planRelease({ root })).toThrow("edited by hand");
  });

  test("a new plugin is released at the version it declares, keeping its notes", () => {
    const marketplace = json(".claude-plugin/marketplace.json");
    marketplace.plugins.push({ description: "three", name: "three", source: "./plugins/three" });
    put(".claude-plugin/marketplace.json", marketplace);
    put("plugins/three/.claude-plugin/plugin.json", { name: "three", version: "1.0.0" });
    put(
      "plugins/three/CHANGELOG.md",
      "# Changelog\n\n## Unreleased\n\n- Later.\n\n## 1.0.0\n\n- First.\n",
    );
    put("README.md", `${read("README.md")}| \`three\` | 1.0.0 | 3 |\n`);
    commit(
      "feat: add three",
      "plugins/three/skills/three/SKILL.md",
      "---\nname: three\ndescription: 3.\n---\n",
    );
    const plan = planRelease({ root });
    expect(plan.releases.map(({ name, previous, version }) => [name, previous, version])).toEqual([
      ["three", null, "1.0.0"],
    ]);
    expect(plan.releases[0].notes).toBe("- Later.\n\n- First.");
  });

  test("the written release updates every version and passes the validator", () => {
    commit(
      "fix: two",
      "plugins/two/skills/two/SKILL.md",
      "---\nname: two\ndescription: 2.\nmetadata:\n  version: 0.3.0\n---\n",
    );
    const files = writeRelease(planRelease({ root }), root);
    expect(files).toEqual([
      "plugins/two/.claude-plugin/plugin.json",
      "plugins/two/CHANGELOG.md",
      "plugins/two/package.json",
      "plugins/two/skills/two/SKILL.md",
      "README.md",
    ]);
    expect(json("plugins/two/package.json").version).toBe("0.3.1");
    expect(read("plugins/two/skills/two/SKILL.md")).toContain("  version: 0.3.1");
    expect(read("README.md")).toContain("| [`two`](plugins/two/README.md) | 0.3.1 |");
    expect(validate(root)).toEqual([]);
  });

  test("a tag becomes the next baseline, and a tag off this history is refused", () => {
    const source = commit("fix: one", "plugins/one/skills/one/SKILL.md");
    writeRelease(planRelease({ root }), root);
    commit(RELEASE_SUBJECT);
    git(["tag", "one--v0.3.1"]);
    expect(planned()).toEqual([]);
    commit("fix: again", "plugins/one/skills/one/SKILL.md");
    expect(planned()).toEqual(["one@0.3.2"]);
    git(["checkout", "--quiet", "--detach", source]);
    expect(planned()).toEqual([]);
    git(["checkout", "--quiet", "-b", "elsewhere", since]);
    put("release.json", { since });
    commit("fix: unrelated", "plugins/one/skills/one/SKILL.md");
    expect(() => planRelease({ root })).toThrow("not in this branch's history");
  });

  test("check gates the release in a scratch worktree and leaves the checkout alone", () => {
    commit("fix: one", "plugins/one/skills/one/SKILL.md");
    put("scratch.txt", "local work\n");
    const status = git(["status", "--porcelain"]);
    let seen;
    const gate = vi.fn((files, dir) => {
      seen = dir;
      expect(dir).not.toBe(root);
      const manifest = fs.readFileSync(
        path.join(dir, "plugins/one/.claude-plugin/plugin.json"),
        "utf8",
      );
      expect(JSON.parse(manifest).version).toBe("0.3.1");
      expect(files).toContain("plugins/one/CHANGELOG.md");
    });
    expect(checkRelease({ gate, root }).releases).toHaveLength(1);
    expect(gate).toHaveBeenCalledOnce();
    expect(fs.existsSync(seen)).toBe(false);
    expect(git(["status", "--porcelain"])).toBe(status);
    expect(git(["worktree", "list"]).split("\n")).toHaveLength(1);
    expect(() =>
      checkRelease({
        gate: () => {
          throw new Error("gate failed");
        },
        root,
      }),
    ).toThrow("gate failed");
    expect(git(["worktree", "list"]).split("\n")).toHaveLength(1);
  });

  describe("publishing", () => {
    let remote, env, created, request;
    const tagsOnRemote = () => git(["ls-remote", "--tags", remote]);
    const branchOnRemote = () => git(["ls-remote", remote, "refs/heads/main"]).split("\t", 1)[0];

    beforeEach(() => {
      const source = commit("fix: ship two", "plugins/two/skills/two/SKILL.md");
      remote = path.join(scratch, "remote.git");
      execFileSync("git", ["init", "--quiet", "--bare", remote]);
      git(["push", "--quiet", remote, "HEAD:refs/heads/main"]);
      env = {
        ...process.env,
        DEFAULT_BRANCH: "main",
        GITHUB_ACTIONS: "true",
        GITHUB_EVENT_NAME: "push",
        GITHUB_REF: "refs/heads/main",
        GITHUB_REPOSITORY: "someone/plugins",
        GITHUB_SERVER_URL: "https://github.com",
        GITHUB_SHA: source,
        GITHUB_TOKEN: "token-for-tests",
      };
      created = new Map();
      request = vi.fn(async (url, options = {}) => {
        if (options.method === "POST") {
          const body = JSON.parse(options.body);
          created.set(body.tag_name, body);
          return new Response("{}", { status: 201 });
        }
        const tag = decodeURIComponent(url.slice(url.lastIndexOf("/") + 1));
        return new Response("{}", { status: created.has(tag) ? 200 : 404 });
      });
    });
    const publish = (overrides = {}) =>
      publishRelease({ env, gate: vi.fn(), remote, request, root, ...overrides });

    test("pushes the release commit with its tags, then creates GitHub releases", async () => {
      expect(await publish()).toBe("Released two--v0.3.1.");
      const released = git(["rev-parse", "HEAD"]);
      expect(branchOnRemote()).toBe(released);
      expect(tagsOnRemote()).toContain("refs/tags/two--v0.3.1");
      expect(git(["show", "-s", "--format=%B", released])).toContain(
        `Release-Of: ${env.GITHUB_SHA}`,
      );
      expect(created.get("two--v0.3.1")).toMatchObject({
        name: "two 0.3.1",
        target_commitish: released,
      });
      expect(request.mock.calls[0][0]).toBe(
        "https://api.github.com/repos/someone/plugins/releases/tags/two--v0.3.1",
      );
    });

    test("a rerun after a failed GitHub call only restores the missing release", async () => {
      request.mockResolvedValueOnce(new Response("", { status: 404 }));
      request.mockResolvedValueOnce(new Response("", { status: 502 }));
      await expect(publish()).rejects.toThrow("HTTP 502");
      git(["checkout", "--quiet", "--detach", env.GITHUB_SHA]);
      expect(await publish()).toContain("Already released");
      expect(created.keys().toArray()).toEqual(["two--v0.3.1"]);
      expect(created.get("two--v0.3.1").body).toContain("fix: ship two");
      expect(git(["tag", "--list"])).toBe("two--v0.3.1");
    });

    test("a newer push wins and publishes both plugins", async () => {
      const newer = commit("feat: grow one", "plugins/one/skills/one/SKILL.md");
      git(["push", "--quiet", remote, "HEAD:refs/heads/main"]);
      git(["checkout", "--quiet", "--detach", env.GITHUB_SHA]);
      expect(await publish()).toContain("moved on");
      git(["checkout", "--quiet", "--detach", newer]);
      env.GITHUB_SHA = newer;
      expect(await publish()).toBe("Released one--v0.4.0, two--v0.3.1.");
    });

    test.each([
      { GITHUB_EVENT_NAME: "pull_request" },
      { GITHUB_REF: "refs/heads/topic" },
      { GITHUB_SHA: "0".repeat(40) },
      { GITHUB_TOKEN: "" },
      { GITHUB_ACTIONS: "" },
    ])("refuses to publish with %j", async (override) => {
      const gate = vi.fn();
      await expect(publish({ env: { ...env, ...override }, gate })).rejects.toThrow(
        "Refusing to publish",
      );
      expect(gate).not.toHaveBeenCalled();
      expect(request).not.toHaveBeenCalled();
    });

    test("nothing is pushed when the gates fail or touch other files", async () => {
      const head = env.GITHUB_SHA;
      const failing = vi.fn(() => {
        throw new Error("gate failed");
      });
      await expect(publish({ gate: failing })).rejects.toThrow("gate failed");
      git(["checkout", "--quiet", "--force", "--detach", head]);
      const straying = vi.fn(() => put("stray.txt", "x\n"));
      await expect(publish({ gate: straying })).rejects.toThrow("stray.txt");
      expect(branchOnRemote()).toBe(head);
      expect(tagsOnRemote()).toBe("");
    });

    test("a push that races the release publishes no tags", async () => {
      const racing = commit("docs: racing", "notes.md");
      git(["checkout", "--quiet", "--detach", env.GITHUB_SHA]);
      const gate = vi.fn(() => git(["push", "--quiet", remote, `${racing}:refs/heads/main`]));
      await expect(publish({ gate })).rejects.toThrow("git push failed");
      expect(branchOnRemote()).toBe(racing);
      expect(tagsOnRemote()).toBe("");
    });
  });
});
