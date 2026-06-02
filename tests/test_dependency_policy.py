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


if __name__ == "__main__":
    unittest.main()
