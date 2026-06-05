import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class DeploymentConfigTests(unittest.TestCase):
    def test_ci_uses_node_24_for_javascript_checks(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        node_versions = re.findall(r'node-version:\s*"([^"]+)"', workflow)

        self.assertRegex(workflow, r"FORCE_JAVASCRIPT_ACTIONS_TO_NODE24:\s+true")
        self.assertNotIn("actions/checkout@v4", workflow)
        self.assertNotIn("actions/setup-node@v4", workflow)
        self.assertNotIn("actions/setup-python@v5", workflow)
        self.assertIn("actions/checkout@v6", workflow)
        self.assertIn("actions/setup-node@v6", workflow)
        self.assertIn("actions/setup-python@v6", workflow)
        self.assertGreaterEqual(len(node_versions), 2)
        self.assertEqual(set(node_versions), {"24"})

    def test_ci_runs_pytest_gate(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

        self.assertRegex(workflow, r"python -m pip install .*--constraint requirements-ci\.txt.*pytest")
        self.assertIn("python -m pytest -q", workflow)
        self.assertNotIn("python -m unittest discover", workflow)

    def test_ci_installs_gmail_extra_with_pinned_constraints(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        constraints = (ROOT / "requirements-ci.txt").read_text(encoding="utf-8")

        self.assertIn('--constraint requirements-ci.txt -e ".[pdf,gmail]"', workflow)
        self.assertIn("from google.oauth2.credentials import Credentials", workflow)
        self.assertIn("from googleapiclient.discovery import build", workflow)
        self.assertIn(
            'python -m pytest -q tests/test_user_store.py tests/test_server.py -k "gmail or google_oauth or oauth or google_identity"',
            workflow,
        )
        self.assertRegex(constraints, re.compile(r"^pytest==\d+\.\d+\.\d+$", re.MULTILINE))
        self.assertRegex(constraints, re.compile(r"^ruff==\d+\.\d+\.\d+$", re.MULTILINE))
        self.assertRegex(constraints, re.compile(r"^pypdf==\d+\.\d+\.\d+$", re.MULTILINE))
        self.assertRegex(constraints, re.compile(r"^google-api-python-client==\d+\.\d+\.\d+$", re.MULTILINE))
        self.assertRegex(constraints, re.compile(r"^google-auth==\d+\.\d+\.\d+$", re.MULTILINE))

    def test_ruff_checks_more_than_pyflakes(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn('select = ["E", "F", "B"]', pyproject)
        self.assertNotIn('select = ["F"]', pyproject)

    def test_render_blueprint_keeps_public_deploy_hardened(self):
        blueprint = (ROOT / "render.yaml").read_text(encoding="utf-8")

        self.assertIn("plan: free", blueprint)
        self.assertIn("startCommand: python -m nda_automation.server --host 0.0.0.0 --port $PORT", blueprint)
        self.assertIn("healthCheckPath: /healthz", blueprint)
        self.assertRegex(blueprint, r"key:\s+NDA_REQUIRE_AUTH\s+value:\s+\"true\"")
        self.assertRegex(blueprint, r"key:\s+NDA_AUTH_PASSWORD\s+sync:\s+false")
        self.assertRegex(blueprint, r"key:\s+NDA_ALLOWED_HOSTS\s+sync:\s+false")
        self.assertRegex(blueprint, r"key:\s+NDA_GOOGLE_OAUTH_CLIENT_ID\s+sync:\s+false")
        self.assertRegex(blueprint, r"key:\s+NDA_GOOGLE_OAUTH_CLIENT_SECRET\s+sync:\s+false")
        self.assertRegex(blueprint, r"key:\s+NDA_GOOGLE_OAUTH_REDIRECT_URI\s+sync:\s+false")
        self.assertRegex(blueprint, r"key:\s+NDA_GMAIL_OAUTH_REDIRECT_URI\s+sync:\s+false")
        self.assertRegex(blueprint, r"key:\s+NDA_DATA_DIR\s+value:\s+/tmp/nda-automation/data")
        self.assertRegex(blueprint, r"key:\s+NDA_USERS_PATH\s+value:\s+/tmp/nda-automation/data/users.json")
        self.assertRegex(blueprint, r"key:\s+NDA_EXPORTS_DIR\s+value:\s+/tmp/nda-automation/exports")
        self.assertRegex(blueprint, r"key:\s+NDA_ALLOW_EPHEMERAL_DATA\s+value:\s+\"true\"")
        self.assertRegex(blueprint, r"key:\s+NDA_RATE_LIMIT_PER_MINUTE\s+value:\s+\"120\"")
        self.assertRegex(blueprint, r"key:\s+NDA_AI_REVIEW_ENABLED\s+value:\s+\"true\"")
        self.assertRegex(blueprint, r"key:\s+NDA_AI_PROVIDER\s+value:\s+gemini")
        self.assertRegex(blueprint, r"key:\s+NDA_AI_MODEL\s+value:\s+gemini-1\.5-flash")
        self.assertRegex(blueprint, r"key:\s+GEMINI_API_KEY\s+sync:\s+false")
        self.assertRegex(blueprint, r"key:\s+NDA_GMAIL_TRIAGE_API_KEY\s+sync:\s+false")
        self.assertRegex(blueprint, r"key:\s+NDA_GMAIL_TRIAGE_MODEL\s+value:\s+qwen/qwen3-32b")
        self.assertNotRegex(blueprint, r"disk:\s+name:\s+nda-automation-data\s+mountPath:\s+/var/data")
