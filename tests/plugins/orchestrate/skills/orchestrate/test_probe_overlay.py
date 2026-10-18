from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[5]
    / "plugins"
    / "orchestrate"
    / "skills"
    / "orchestrate"
    / "scripts"
    / "probe_overlay.py"
)
SPEC = importlib.util.spec_from_file_location("probe_overlay", SCRIPT)
assert SPEC and SPEC.loader
PROBE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBE)


class ProbeOverlayTests(unittest.TestCase):
    def make_repo(self, root: Path) -> Path:
        repo = root / "repo"
        repo.mkdir()
        (repo / "node_modules").mkdir()
        (repo / "node_modules" / "marker.txt").write_text("dependency", encoding="utf-8")
        (repo / "package.json").write_text('{"name":"source"}\n', encoding="utf-8")
        (repo / "src").mkdir()
        (repo / "src" / "target.py").write_text("value = 'original'\n", encoding="utf-8")
        return repo

    def test_copies_only_requested_files_links_shared_inputs_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self.make_repo(root)
            with PROBE.probe_overlay(repo, copy_paths=["src/target.py"]) as overlay:
                self.assertTrue(overlay.name.startswith("ork-probe-"))
                self.assertEqual(overlay.parent, Path("/tmp").resolve())
                self.assertEqual((overlay / "src/target.py").read_text(encoding="utf-8"), "value = 'original'\n")
                self.assertFalse((overlay / "src/other.py").exists())
                self.assertTrue((overlay / "node_modules").is_symlink())
                self.assertEqual((overlay / "node_modules/marker.txt").read_text(encoding="utf-8"), "dependency")
                self.assertTrue((overlay / "package.json").is_symlink())
                (overlay / "src/target.py").write_text("value = 'candidate'\n", encoding="utf-8")
                self.assertEqual((repo / "src/target.py").read_text(encoding="utf-8"), "value = 'original'\n")
                overlay_path = overlay

            self.assertFalse(overlay_path.exists())

    def test_candidate_patch_modifies_only_the_overlay_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self.make_repo(root)
            patch = root / "candidate.patch"
            patch.write_text(
                "diff --git a/src/target.py b/src/target.py\n"
                "--- a/src/target.py\n"
                "+++ b/src/target.py\n"
                "@@ -1 +1 @@\n"
                "-value = 'original'\n"
                "+value = 'patched'\n",
                encoding="utf-8",
            )

            with PROBE.probe_overlay(repo, copy_paths=["src/target.py"], patch_path=patch) as overlay:
                self.assertEqual((overlay / "src/target.py").read_text(encoding="utf-8"), "value = 'patched'\n")
                self.assertEqual((repo / "src/target.py").read_text(encoding="utf-8"), "value = 'original'\n")

    def test_cli_runs_from_overlay_and_cleans_up_after_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self.make_repo(root)
            result_path = root / "result.json"
            command = [
                sys.executable,
                "-c",
                "import json, os, pathlib; p=pathlib.Path(os.environ['ORK_PROBE_OVERLAY']); "
                "pathlib.Path(os.environ['RESULT_PATH']).write_text(json.dumps({'cwd': str(pathlib.Path.cwd()), "
                "'overlay': str(p), 'tmp': os.environ['TMPDIR'], 'cache': os.environ['XDG_CACHE_HOME']}))",
            ]
            environment_result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--repo",
                    str(repo),
                    "--copy",
                    "src/target.py",
                    "--",
                    *command,
                ],
                capture_output=True,
                text=True,
                env={**os.environ, "RESULT_PATH": str(result_path)},
                check=False,
            )

            self.assertEqual(environment_result.returncode, 0, environment_result.stderr)
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(Path(result["cwd"]).resolve(), Path(result["overlay"]).resolve())
            self.assertTrue(Path(result["overlay"]).name.startswith("ork-probe-"))
            self.assertTrue(Path(result["tmp"]).is_relative_to(Path(result["overlay"])))
            self.assertTrue(Path(result["cache"]).is_relative_to(Path(result["overlay"])))
            self.assertFalse(Path(result["overlay"]).exists())

    def test_rejects_paths_outside_repo_and_requires_an_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self.make_repo(root)
            for invalid in ("../outside.txt", str(root / "absolute.txt")):
                with self.subTest(path=invalid), self.assertRaisesRegex(ValueError, "relative"):
                    with PROBE.probe_overlay(repo, copy_paths=[invalid]):
                        self.fail("invalid path should not create an overlay")
            with self.assertRaisesRegex(ValueError, "at least one"):
                with PROBE.probe_overlay(repo):
                    self.fail("empty overlay should not be created")

    def test_rejects_patch_that_adds_a_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self.make_repo(root)
            patch = root / "symlink.patch"
            patch.write_text(
                "diff --git a/escape b/escape\n"
                "new file mode 120000\n"
                "--- /dev/null\n"
                "+++ b/escape\n"
                "@@ -0,0 +1 @@\n"
                "+../repo/src/target.py\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "symlinks"):
                with PROBE.probe_overlay(repo, patch_path=patch):
                    self.fail("symlink patch should be rejected")
            self.assertEqual((repo / "src/target.py").read_text(encoding="utf-8"), "value = 'original'\n")

    def test_rejects_patch_paths_that_escape_the_overlay(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self.make_repo(root)
            patch = root / "escape.patch"
            escaped = root / "escaped.txt"
            patch.write_text(
                "diff --git a/../../escaped.txt b/../../escaped.txt\n"
                "new file mode 100644\n"
                "--- /dev/null\n"
                "+++ b/../../escaped.txt\n"
                "@@ -0,0 +1 @@\n"
                "+should not be written\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "could not be applied"):
                with PROBE.probe_overlay(repo, patch_path=patch):
                    self.fail("escaping patch should be rejected")
            self.assertFalse(escaped.exists())


if __name__ == "__main__":
    unittest.main()
