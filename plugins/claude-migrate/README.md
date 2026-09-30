# claude-migrate

Moves your whole Claude Code setup to a new Mac in one encrypted archive, and tells you what is
still missing once it is restored.

Claude Code has no export command of its own (`claude import` only pulls config in from Codex,
Gemini and Cursor), and copying `~/.claude` by hand misses things: a second profile, the skill
hub its links point into, the absolute paths in hooks, and project folders named after your home
directory.

## Use

On the old Mac:

```sh
claude-migrate export --dry-run   # see what goes in and what stays out
claude-migrate export             # asks for a passphrase
```

This writes `~/claude-migration-<host>-<time>.tar.gz.enc` and a `-README.txt` beside it. Copy
both to the new Mac, then follow the README:

```sh
curl -fsSL https://claude.ai/install.sh | bash
openssl enc -d -aes-256-cbc -md sha256 -pbkdf2 -iter 200000 -in claude-migration-….tar.gz.enc | tar -xzf -
bash claude-migration-…/claude-migrate import claude-migration-…
```

Import prints `claude-migrate doctor`'s report when it finishes. Work down the list, run
`claude-migrate doctor` again, and repeat until it reports nothing missing.

| Option                   |                                                                                                                |
| ------------------------ | -------------------------------------------------------------------------------------------------------------- |
| `--no-history`           | Leave out transcripts (`claude --resume`), prompt history and `/rewind` checkpoints. Memory still comes along. |
| `--no-encrypt`           | Plain `.tar.gz`. The dry run lists files holding token-shaped strings first.                                   |
| `-o DIR`                 | Output directory (default `~`).                                                                                |
| `--passphrase-file FILE` | For scripted runs; otherwise it asks on the terminal.                                                          |
| `import --dry-run`       | Unpack and report what would change, without changing anything.                                                |
| `import --yes`           | Skip the confirmation.                                                                                         |

## What moves

- Every profile: `~/.claude` and any `~/.claude-*` (`CLAUDE_CONFIG_DIR`) directory, plus
  `~/.claude.json`. That covers settings, `CLAUDE.md`, rules, hooks, skills, agents, plugins and
  marketplaces, memory, transcripts, prompt history, plans and tasks.
- `~/.agents` (the shared skill hub), `~/.config/agent-executor`, `~/.config/ai-usage`.
- The Claude launchers in `~/.local/bin` (`plaude`, `claude-*`), and the Claude desktop app's
  `claude_desktop_config.json`.
- Your shell files, as reference copies only. RESTORE.md quotes their Claude lines.

Symlinks stay symlinks. Caches, logs, Python virtualenvs, runtime state, org-pushed policy files
and old backup copies stay behind; the dry run lists each one with its size.

## What it does not move

- **Logins.** They live in the macOS Keychain. Log in to each profile again; doctor checks.
- **Git repos.** The archive records each repo your links, marketplaces, hooks and MCP servers
  point into, with its remote, branch and install scripts. Doctor prints the `git clone` lines.
- **Programs** such as Homebrew's `node` or Vibe Island. RESTORE.md lists them.

## If the home directory changes

Say you were `/Users/old` and are now `/Users/new`. Import then rewrites the old path everywhere
first: file contents (settings, hooks, `~/.claude.json`, transcripts), link targets, and the
project folders Claude Code names after each project's path (`-Users-old-…`). That keeps
`claude --resume` and memory attached to the right projects.

## Safety

- Nothing in `$HOME` changes until you confirm.
- Anything the import would overwrite is first copied to
  `~/.local/share/claude-migrate/backups/<time>/`.
- Files that only the new Mac has are kept.
- The archive is AES-256 encrypted with a PBKDF2-derived key. Stock macOS LibreSSL can decrypt it,
  so the new Mac needs nothing installed first.
- Needs only what a fresh Mac ships with: bash 3.2, bsdtar, sed, grep, awk and openssl.

## Install

```sh
./install.sh   # links ~/.local/bin/claude-migrate to this checkout
```
