# Releases

Plugin versions are never edited by hand. Every push to `main` that passes CI
runs `scripts/release.js publish`, which works out which plugins changed and
releases each of them.

## How a version is chosen

For each plugin, the script looks at the commits since its last tag
(`<plugin>--vX.Y.Z`) that touched `plugins/<plugin>/` or its marketplace entry.
`CHANGELOG.md` edits do not count, and a change that was later reverted
releases nothing.

| Commits since the last release              | Next version                             |
| ------------------------------------------- | ---------------------------------------- |
| any `feat:`                                 | minor                                    |
| any `type!:` or a `BREAKING CHANGE:` footer | major, or minor while the version is 0.x |
| anything else                               | patch                                    |

A plugin that has never been released goes out at the version its `plugin.json`
declares. `release.json` names the commit that counting starts from for plugins
without a tag.

## Release notes

The commit subjects become the changelog entry. To write the notes yourself, add
them under a `## Unreleased` heading at the top of the plugin's `CHANGELOG.md`;
the release turns that heading into the version.

## Trying it locally

```sh
node scripts/release.js plan    # what would be released, as JSON
node scripts/release.js check   # build it in a scratch worktree and run the lint gate on it
```

Run `git fetch --tags` first so the script sees the latest releases. CI runs
`check` on every push and pull request, so a hand-edited version fails there.

## What publishing does

1. Confirms it is running in GitHub Actions for a push to the default branch,
   on the exact commit that was tested.
2. Writes the new versions into `plugin.json`, the plugin's `package.json`, any
   SKILL.md `metadata.version` and the README table, and adds the changelog
   section.
3. Formats those files and runs the lint gate and all tests again.
4. Commits them as `chore(release): publish plugins`, with a `Release-Of:`
   trailer naming the tested commit, and pushes the commit and its tags in one
   atomic push.
5. Creates a GitHub release for each tag.

If someone pushes while this runs, the push is rejected and nothing is
published; the run for the newer commit releases everything. If the tags went
out but creating a GitHub release failed, rerunning the job creates only the
missing releases.

## Repository settings

The job pushes with the workflow's `GITHUB_TOKEN`. Under **Settings → Actions →
General → Workflow permissions**, choose **Read and write permissions**. If
`main` is protected, create a fine-grained token with read and write access to
this repository's contents, allow it to bypass the protection, and store it as
the `RELEASE_TOKEN` secret; the job uses it instead.
