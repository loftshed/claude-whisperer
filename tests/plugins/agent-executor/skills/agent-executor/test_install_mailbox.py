"""User-visible mailbox setup, preservation and scoped hook trust."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[5] / "plugins/agent-executor/skills/agent-executor/scripts"
sys.path.insert(0, str(SCRIPTS))
import install_mailbox as installer  # noqa: E402 -- exercise the shipped installer


class InstallMailboxTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.path = self.base / "settings.json"
        self.script = Path("/home/example/skill hub/peer_mailbox.py")

    def test_jsonc_comments_and_trailing_commas_preserve_strings(self):
        path = self.base / "config.jsonc"
        path.write_text('{// note\n"url":"https://example.test/a/*b*/", /* comment */ "literal":",}", "items":[1,],}')
        self.assertEqual(
            installer.read_json_config(path), {"url": "https://example.test/a/*b*/", "literal": ",}", "items": [1]}
        )

    def test_hooks_preserve_other_settings_and_back_up_the_previous_contents(self):
        original = '{"permissions":{"deny":["Bash(rm *)"]},"hooks":{"Stop":[{"hooks":[{"type":"command","command":"bailout stop"}]}]}}\n'
        self.path.write_text(original)
        installer.install_hooks(self.path, self.script, "codex")
        settings = json.loads(self.path.read_text())
        self.assertEqual(settings["permissions"], {"deny": ["Bash(rm *)"]})
        self.assertEqual(settings["hooks"]["Stop"][0], {"hooks": [{"type": "command", "command": "bailout stop"}]})
        self.assertEqual(
            settings["hooks"]["SessionStart"],
            [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python3 '/home/example/skill hub/peer_mailbox.py' hook --harness codex",
                            "timeout": 10,
                        }
                    ]
                }
            ],
        )
        self.assertEqual(settings["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"], 3)
        self.assertEqual([p.read_text() for p in self.base.glob("settings.json.mailbox-bak.*")], [original])

    def test_reinstall_does_not_duplicate_hooks_or_rewrite_files(self):
        installer.install_hooks(self.path, self.script, "claude")
        before = self.path.read_bytes()
        self.assertFalse(installer.install_hooks(self.path, self.script, "claude"))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(json.loads(before)["hooks"]["Stop"]), 1)

    def test_native_plugin_can_create_hub_without_removing_old_content(self):
        skill = self.base / "plugin/skills/agent-executor"
        (skill / "scripts").mkdir(parents=True)
        (skill / "scripts/mailbox_mcp.py").write_text("# server")
        hub = self.base / "hub"
        hub.mkdir()
        (hub / "notes.txt").write_text("keep this")
        installer.ensure_hub(hub, skill, dry_run=True)
        self.assertFalse(hub.is_symlink())
        installer.ensure_hub(hub, skill)
        self.assertEqual(hub.resolve(), skill)
        self.assertEqual(next(self.base.glob("hub.mailbox-bak.*/notes.txt")).read_text(), "keep this")
        installer.ensure_hub(hub, skill)
        self.assertEqual(len(list(self.base.glob("hub.mailbox-bak.*"))), 1)

    def test_codex_permissions_cover_only_known_mailbox_tools_and_are_idempotent(self):
        server = {"tools": {"send": {"approval_mode": "approve"}, "unrelated": {"approval_mode": "prompt"}}}
        edits = installer.codex_tool_edits(server)
        self.assertEqual(
            {edit["keyPath"] for edit in edits},
            {
                f"mcp_servers.peer-mailbox.tools.{tool}.approval_mode"
                for tool in ["peers", "inbox", "wait", "ack", "focus"]
            },
        )
        self.assertTrue(all(edit["value"] == "approve" for edit in edits))
        self.assertEqual(server["tools"]["unrelated"], {"approval_mode": "prompt"})
        configured = {"tools": {tool: {"approval_mode": "approve"} for tool in installer.TOOLS}}
        self.assertEqual(installer.codex_tool_edits(configured), [])

    def test_diagnostics_explain_missing_registration_and_delivery_hooks(self):
        status = installer.configuration_status(self.base, {"claude"})
        self.assertFalse(status["claude"]["configured"])
        self.assertIn("mailbox configuration could not be read", status["claude"]["issues"])
        self.assertIn("mailbox turn hooks missing or unreadable", status["claude"]["issues"])

    def test_diagnostics_accept_native_opencode_nested_configuration(self):
        from unittest.mock import patch

        config = self.base / ".config/opencode/opencode.jsonc"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {
                    "mcp": {
                        "servers": {
                            "peer-mailbox": {
                                "command": [
                                    "python3",
                                    str(self.base / ".agents/skills/agent-executor/scripts/mailbox_mcp.py"),
                                ]
                            }
                        }
                    }
                }
            )
        )
        with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(self.base / ".config")}):
            self.assertTrue(installer.configuration_status(self.base, {"opencode"})["opencode"]["configured"])

    def test_dry_run_leaves_settings_intact_then_real_install_adds_delivery(self):
        self.path.write_text('{"model":"example"}\n')
        installer.install_hooks(self.path, self.script, "claude", dry_run=True)
        self.assertEqual(self.path.read_text(), '{"model":"example"}\n')
        installer.install_hooks(self.path, self.script, "claude")
        self.assertEqual(
            set(json.loads(self.path.read_text())["hooks"]), {"SessionStart", "SessionEnd", "Stop", "UserPromptSubmit"}
        )

    def test_wait_timeout_preserves_comments_and_unrelated_configuration(self):
        self.path = self.base / "config.toml"
        self.path.write_text(
            '# user comment\nmodel = "example"\n\n[mcp_servers.peer-mailbox]\ncommand = "python3"\nargs = ["mailbox_mcp.py"]\n\n[other]\nvalue = 7\n'
        )
        installer.set_codex_timeout(self.path)
        self.assertEqual(
            self.path.read_text(),
            '# user comment\nmodel = "example"\n\n[mcp_servers.peer-mailbox]\ntool_timeout_sec = 660\ncommand = "python3"\nargs = ["mailbox_mcp.py"]\n\n[other]\nvalue = 7\n',
        )
        before = self.path.read_bytes()
        installer.set_codex_timeout(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_trust_selects_only_our_four_exact_commands(self):
        command = "python3 '/home/example/skill hub/peer_mailbox.py' hook --harness codex"
        hooks = [
            {
                "key": event,
                "eventName": event,
                "handlerType": "command",
                "sourcePath": str(self.path),
                "command": command,
                "currentHash": "sha256:" + event,
            }
            for event in ["sessionStart", "userPromptSubmit", "stop", "sessionEnd"]
        ]
        hooks.append(
            {
                "key": "unrelated",
                "eventName": "stop",
                "handlerType": "command",
                "sourcePath": str(self.path),
                "command": "unrelated hook",
                "currentHash": "sha256:unrelated",
            }
        )
        self.assertEqual(
            installer.mailbox_hook_updates({"data": [{"hooks": hooks}]}, self.script, self.path),
            {
                "sessionStart": {"trusted_hash": "sha256:sessionStart"},
                "userPromptSubmit": {"trusted_hash": "sha256:userPromptSubmit"},
                "stop": {"trusted_hash": "sha256:stop"},
                "sessionEnd": {"trusted_hash": "sha256:sessionEnd"},
            },
        )
        hooks[0]["command"] += " ; run something else"
        with self.assertRaisesRegex(ValueError, "exactly four"):
            installer.mailbox_hook_updates({"data": [{"hooks": hooks}]}, self.script, self.path)


if __name__ == "__main__":
    unittest.main()
