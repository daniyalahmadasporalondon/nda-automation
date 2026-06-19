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
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("plan: standard", blueprint)
        self.assertIn("runtime: docker", blueprint)
        self.assertIn('CMD ["sh", "-c", "python -m nda_automation.server --host 0.0.0.0 --port ${PORT:-8787}"]', dockerfile)
        self.assertIn("healthCheckPath: /healthz", blueprint)
        self.assertRegex(blueprint, r"key:\s+NDA_REQUIRE_AUTH\s+value:\s+\"true\"")
        self.assertRegex(blueprint, r"key:\s+NDA_AUTH_PASSWORD\s+sync:\s+false")
        # HTTP Basic is a break-glass operator credential, not a shared baked-in
        # identity; the username is operator-set so it never collapses tenants.
        self.assertRegex(blueprint, r"key:\s+NDA_AUTH_USERNAME\s+sync:\s+false")
        self.assertNotRegex(blueprint, r"key:\s+NDA_AUTH_USERNAME\s+value:\s+nda-admin")
        self.assertRegex(blueprint, r"key:\s+NDA_ADMIN_USERS\s+sync:\s+false")
        self.assertRegex(blueprint, r"key:\s+NDA_TRUSTED_PROXY_COUNT\s+value:\s+\"1\"")
        self.assertRegex(blueprint, r"key:\s+NDA_ENFORCE_CSRF\s+value:\s+\"true\"")
        self.assertRegex(blueprint, r"key:\s+NDA_ALLOWED_HOSTS\s+sync:\s+false")
        self.assertRegex(blueprint, r"key:\s+NDA_GOOGLE_OAUTH_CLIENT_ID\s+sync:\s+false")
        self.assertRegex(blueprint, r"key:\s+NDA_GOOGLE_OAUTH_CLIENT_SECRET\s+sync:\s+false")
        self.assertRegex(blueprint, r"key:\s+NDA_GOOGLE_OAUTH_REDIRECT_URI\s+sync:\s+false")
        self.assertRegex(blueprint, r"key:\s+NDA_GMAIL_OAUTH_REDIRECT_URI\s+sync:\s+false")
        # Durable state now lives on the persistent disk at /var/data (Render
        # Standard plan). The startup storage guard treats /var/data as
        # non-ephemeral, so the NDA_ALLOW_EPHEMERAL_DATA escape hatch is removed
        # entirely and the guard is active: assert the flag is ABSENT rather than
        # "true", which is the stronger durability guarantee.
        self.assertRegex(blueprint, r"key:\s+NDA_DATA_DIR\s+value:\s+/var/data")
        self.assertRegex(blueprint, r"key:\s+NDA_USERS_PATH\s+value:\s+/var/data/users.json")
        self.assertRegex(blueprint, r"key:\s+NDA_EXPORTS_DIR\s+value:\s+/var/data/exports")
        # No `key: NDA_ALLOW_EPHEMERAL_DATA` env-var entry (a mention in an
        # explanatory comment documenting its removal is fine and expected).
        self.assertNotRegex(blueprint, r"key:\s+NDA_ALLOW_EPHEMERAL_DATA")
        self.assertRegex(blueprint, r"key:\s+NDA_RATE_LIMIT_PER_MINUTE\s+value:\s+\"120\"")
        self.assertRegex(blueprint, r"key:\s+NDA_AI_REVIEW_ENABLED\s+value:\s+\"true\"")
        self.assertRegex(blueprint, r"key:\s+NDA_AI_PROVIDER\s+value:\s+openrouter")
        self.assertRegex(blueprint, r"key:\s+NDA_AI_MODEL\s+value:\s+anthropic/claude-opus-4.8-fast")
        # Generation uses a FAST model (DeepSeek V4 Flash), separate from the Opus
        # reviewer, so a synchronous generate stays well under the frontend's 45s
        # timeout. Flash is the faster of the two DeepSeek models we run.
        self.assertRegex(blueprint, r"key:\s+NDA_GENERATION_MODEL\s+value:\s+deepseek/deepseek-v4-flash")
        # The generation prose-polish AI is DISABLED on prod for reliability: the
        # clause adapter only rephrases already-on-position clauses, so the pure
        # deterministic path is the same Playbook-compliant doc, instant and with
        # zero OpenRouter calls. Flip to "true" to re-enable (code defaults ON).
        self.assertRegex(blueprint, r"key:\s+NDA_GENERATION_AI_ENABLED\s+value:\s+\"false\"")
        self.assertRegex(blueprint, r"key:\s+OPENROUTER_API_KEY\s+sync:\s+false")
        self.assertNotRegex(blueprint, r"key:\s+GROQ_API_KEY")
        self.assertRegex(blueprint, r"key:\s+NDA_GMAIL_TRIAGE_MODEL\s+value:\s+deepseek/deepseek-v4-pro")
        # Generation's AI clause-adaptation budget is env-tunable; prod sets 12s (code
        # default 8s) to preserve more AI rephrasing on prod's slower network while
        # staying under the frontend's 45s generate timeout.
        self.assertRegex(blueprint, r"key:\s+NDA_GENERATION_ADAPT_BUDGET_SECONDS\s+value:\s+\"12\"")
        # Generation pins OpenRouter to the lowest-latency upstream provider so an
        # intermittent slow upstream can't drag the deadline-bounded parallel adapt
        # step to its budget ceiling (the real cause of the 45s generate timeout).
        self.assertRegex(blueprint, r"key:\s+NDA_GENERATION_PROVIDER_SORT\s+value:\s+latency")
        # A persistent disk is mounted at /var/data so users.json, matters, and
        # exports survive restarts/redeploys (the whole point of the Standard plan).
        self.assertRegex(blueprint, r"disk:\s+name:\s+nda-data\s+mountPath:\s+/var/data\s+sizeGB:\s+1")
