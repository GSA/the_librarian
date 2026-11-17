"""Documentation paths, commands and links are current and packaged."""

import re
import subprocess
import sys
import unittest

from tools import package
from tests.support import ROOT


class Documentation(unittest.TestCase):
    def test_component_map_paths_and_documented_module_commands_are_current(self):
        readme = (ROOT / "README.md").read_text()
        manifest = dict(package.release_paths(ROOT))
        for path in re.findall(r"`((?:tools|tests|docs)/[^` ]+/?)`", readme):
            if ".local" not in path:
                with self.subTest(path=path):
                    prefix = path.rstrip("/") + "/"
                    self.assertTrue(path in manifest or any(item.startswith(prefix) for item in manifest), path)
        modules = set(re.findall(
            r"(?:\.venv/bin/)?python\d* -m ([a-zA-Z0-9_.]+)", readme
        ))
        self.assertIn("tools.catalog.catalog_store", modules)
        for module in modules - {"pip", "venv"}:
            with self.subTest(module=module):
                result = subprocess.run(
                    [sys.executable, "-m", module, "--help"], cwd=ROOT,
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_librarian_identity_and_role_documents_are_packaged(self):
        paths = dict(package.release_paths(ROOT))
        self.assertEqual((ROOT / "README.md").read_text().splitlines()[0], "# The Librarian")
        self.assertTrue({
            "ROADMAP.md", "agents/librarian.md", "agents/contributions.md",
            "agents/refiner.md", "agents/coder.md",
        }.issubset(paths))

    def test_retired_execution_documents_are_not_packaged(self):
        paths = dict(package.release_paths(ROOT))
        for name in (
            "docs/superpowers/plans/2026-10-20-docling-ingestion.md",
            "docs/superpowers/plans/2026-10-22-ingestion-completion.md",
            "docs/superpowers/specs/2026-10-20-docling-ingestion-design.md",
            "docs/superpowers/specs/2026-10-22-ingestion-completion-design.md",
        ):
            self.assertNotIn(name, paths)

    def test_relative_document_links_are_packaged(self):
        paths = dict(package.release_paths(ROOT))
        for name, source in paths.items():
            if source.suffix != ".md":
                continue
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", source.read_text()):
                if "://" in target or target.startswith("#"):
                    continue
                linked = (source.parent / target.split("#", 1)[0]).resolve()
                with self.subTest(document=name, target=target):
                    self.assertIn(linked.relative_to(ROOT).as_posix(), paths)


if __name__ == "__main__":
    unittest.main()
