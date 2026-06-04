import re
import unittest
from pathlib import Path


class DependencyPolicyTests(unittest.TestCase):
    def test_pdf_dependency_is_optional_not_core(self):
        pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
        project_dependencies = re.search(
            r"(?ms)^\[project\].*?^dependencies\s*=\s*\[(.*?)\]",
            pyproject,
        )
        self.assertIsNotNone(project_dependencies)
        self.assertNotIn("pypdf", project_dependencies.group(1))
        self.assertRegex(pyproject, r'(?ms)^\[project\.optional-dependencies\].*?^pdf\s*=\s*\[.*?"pypdf>=6\.0"')

    def test_gmail_dependency_is_optional_not_core(self):
        pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
        project_dependencies = re.search(
            r"(?ms)^\[project\].*?^dependencies\s*=\s*\[(.*?)\]",
            pyproject,
        )

        self.assertIsNotNone(project_dependencies)
        self.assertNotIn("google-api-python-client", project_dependencies.group(1))
        self.assertRegex(
            pyproject,
            r'(?ms)^\[project\.optional-dependencies\].*?^gmail\s*=\s*\[.*?"google-api-python-client>=2\.0"',
        )
        self.assertRegex(pyproject, r'(?ms)^gmail\s*=\s*\[.*?"google-auth>=2\.0"')

    def test_gmail_oauth_dependencies_are_importable_for_ci_suite(self):
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        self.assertIsNotNone(Credentials)
        self.assertIsNotNone(build)


if __name__ == "__main__":
    unittest.main()
