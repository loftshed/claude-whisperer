"""Serve a disposable, frozen mailbox for read-only MCP evaluation questions."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[5] / "plugins/agent-executor/skills/agent-executor/scripts"
sys.path.insert(0, str(SCRIPTS))
import mailbox_mcp  # noqa: E402 -- test-only stdio fixture imports the deployed entry point
import peer_mailbox  # noqa: E402 -- test-only fixture writes synthetic data


def main():
    fixture = json.loads((Path(__file__).parent / "evaluations/snapshot.json").read_text())
    with tempfile.TemporaryDirectory(prefix="mailbox-evaluation-") as folder:
        repository = Path(folder) / "project"
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        os.chdir(repository)
        os.environ["AGENT_EXECUTOR_HOME"] = str(Path(folder) / "cache")
        os.environ["AGENT_EXECUTOR_FEEDBACK_DIR"] = str(Path(folder) / "feedback")
        for key in ("CLAUDE_CODE_SESSION_ID", "AGENT_MAILBOX_SESSION", "AGENT_MAILBOX_LISTENER_SESSION"):
            os.environ.pop(key, None)
        base = peer_mailbox.root()
        sender_records = {}
        for peer in fixture["peers"]:
            record = peer_mailbox.register(
                base, session=peer["session"], harness=peer["harness"], cwd=str(repository), pid=os.getpid()
            )
            record.update(name=peer["name"], focus=peer["focus"])
            peer_mailbox.write(peer_mailbox.peer_path(base, peer["session"]), record)
            sender_records[peer["session"]] = record
        recipient = peer_mailbox.register(
            base, session="investigator", harness="codex", cwd=str(repository), pid=os.getpid()
        )
        for message in fixture["messages"]:
            peer_mailbox.write(
                peer_mailbox.inbox_dir(base, recipient["session"]) / (message["id"] + ".json"),
                {
                    "schema": peer_mailbox.MAIL_SCHEMA,
                    "id": message["id"],
                    "from": peer_mailbox.sender_address(sender_records[message["sender"]]),
                    "to": {"session": recipient["session"], "name": recipient["name"]},
                    "sentAt": message["sentAt"],
                    "replyTo": message["replyTo"],
                    "text": message["text"],
                    "readAt": None,
                    "scope": "project",
                },
            )
        # Evaluation questions may only inspect data. Keep the actual server/transport and disable writes.
        original = mailbox_mcp.Server.execute

        def read_only(server, name, arguments, meta, cancelled=None):
            if name not in {"peers", "inbox"} or name == "inbox" and arguments.get("markRead", True):
                raise peer_mailbox.MailboxError("evaluation supports peers and inbox(markRead:false) only")
            if arguments.get("session") != "investigator":
                raise peer_mailbox.MailboxError("use session=investigator for this evaluation")
            return original(server, name, arguments, meta, cancelled)

        mailbox_mcp.Server.execute = read_only
        return mailbox_mcp.main()


if __name__ == "__main__":
    raise SystemExit(main())
