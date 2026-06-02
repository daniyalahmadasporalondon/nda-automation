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

    def test_render_blueprint_keeps_public_deploy_hardened(self):
        blueprint = (ROOT / "render.yaml").read_text(encoding="utf-8")

        self.assertIn("plan: starter", blueprint)
        self.assertIn("startCommand: python -m nda_automation.server --host 0.0.0.0 --port $PORT", blueprint)
        self.assertIn("healthCheckPath: /healthz", blueprint)
        self.assertRegex(blueprint, r"key:\s+NDA_REQUIRE_AUTH\s+value:\s+\"true\"")
        self.assertRegex(blueprint, r"key:\s+NDA_AUTH_PASSWORD\s+sync:\s+false")
        self.assertRegex(blueprint, r"key:\s+NDA_DATA_DIR\s+value:\s+/var/data")
        self.assertRegex(blueprint, r"key:\s+NDA_EXPORTS_DIR\s+value:\s+/var/data/exports")
        self.assertRegex(blueprint, r"key:\s+NDA_RATE_LIMIT_PER_MINUTE\s+value:\s+\"120\"")
        self.assertRegex(blueprint, r"disk:\s+name:\s+nda-automation-data\s+mountPath:\s+/var/data")
