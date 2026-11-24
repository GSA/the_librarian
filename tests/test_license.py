"""The repository carries GSA's default public-domain/CC0 license and says so everywhere it ships."""

import tomllib
import unittest

from tools import package
from tests.support import ROOT

ADAPTER = ROOT / "tools" / "context-mcp"


class LicenseTests(unittest.TestCase):
    def test_license_is_the_gsa_default_text(self):
        text = (ROOT / "LICENSE.md").read_text()
        self.assertTrue(text.startswith(
            "As a work of the United States government, this project is in the\n"
            "public domain within the United States.\n"))
        self.assertIn("CC0 1.0 Universal public domain dedication", text)
        self.assertIn("https://creativecommons.org/publicdomain/zero/1.0/legalcode", text)

    def test_contributing_and_security_point_at_the_license_and_gsa_policy(self):
        contributing = (ROOT / "CONTRIBUTING.md").read_text()
        self.assertIn("released under the CC0", contributing)
        self.assertIn("[LICENSE](LICENSE.md)", contributing)
        security = (ROOT / "SECURITY.md").read_text()
        self.assertIn("GSA Vulnerability Disclosure", security)
        self.assertIn("https://www.gsa.gov/website-information/vulnerability-disclosure-policy", security)

    def test_readme_has_a_license_section_linking_the_file(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn("\n## License\n", readme)
        self.assertIn("[LICENSE.md](LICENSE.md)", readme)
        self.assertIn("CC0", readme)

    def test_adapter_metadata_declares_cc0_and_ships_the_license(self):
        project = tomllib.loads((ADAPTER / "pyproject.toml").read_text())["project"]
        self.assertEqual(project["license"], "CC0-1.0")
        self.assertEqual(project["license-files"], ["LICENSE.md"])
        # setuptools cannot reach above the package directory, so the adapter carries an identical copy.
        self.assertEqual((ADAPTER / "LICENSE.md").read_bytes(), (ROOT / "LICENSE.md").read_bytes())

    def test_license_files_are_packaged(self):
        paths = dict(package.release_paths(ROOT))
        for name in ("LICENSE.md", "CONTRIBUTING.md", "SECURITY.md", "tools/context-mcp/LICENSE.md",
                     "tests/test_license.py"):
            self.assertIn(name, paths)

    def test_third_party_licenses_are_recorded_where_the_dependencies_are_installed(self):
        readme = (ROOT / "tools/extraction/README.md").read_text()
        self.assertIn("## Third-party licenses", readme)
        for name in ("Docling", "RapidOCR", "TableFormer", "PDFium"):
            self.assertIn(name, readme)
        self.assertIn("not redistributed", readme)


if __name__ == "__main__":
    unittest.main()
