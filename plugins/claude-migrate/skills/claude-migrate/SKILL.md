---
name: claude-migrate
description: Move the user's Claude Code setup to a new Mac or restore it there. Use when the user mentions a new laptop, moving machines, backing up or exporting their Claude settings, skills, memory or history, restoring them, or checking what is still missing after a restore.
---

# claude-migrate

`claude-migrate` (the plugin's `bin/claude-migrate`; `install.sh` links it as `~/.local/bin/claude-migrate`) packs
every Claude Code profile into one encrypted archive and restores it on another Mac.

## Old Mac

```sh
claude-migrate export --dry-run   # what goes in, what stays out, repos to clone, token scan
claude-migrate export             # asks for a passphrase; writes ~/claude-migration-<host>-<time>.tar.gz.enc
```

- `--no-history` leaves out transcripts, prompt history and /rewind checkpoints (memory stays).
- `-o DIR` picks the output directory; `--no-encrypt` writes a plain `.tar.gz`.
- It needs a terminal for the passphrase. From an agent shell without one, give the user the
  command to run with a `!` prefix instead of creating a passphrase file for them.

## New Mac

The archive carries the script. `<name>-README.txt` next to it has the three commands:
install Claude Code, decrypt with `openssl enc -d …`, then `bash <dir>/claude-migrate import <dir>`.
Import asks before changing anything and copies whatever it overwrites to
`~/.local/share/claude-migrate/backups/<time>`. Then `claude-migrate doctor` lists what is left:
profile logins, repos to clone and their install scripts, links with no target, and programs the
hooks, status line and MCP servers call. The full checklist, including the old shell aliases,
is `~/.local/share/claude-migrate/last-import/RESTORE.md`.

## What it does not move

Keychain logins (Claude, gh, glab, Codex, Antigravity), git repos (it records where
to clone them from), Homebrew and other programs, and shell config (kept as reference copies in
the archive; never installed).
