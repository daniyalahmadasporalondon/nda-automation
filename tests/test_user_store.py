from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nda_automation import matter_store, user_store


class UserStoreTests(unittest.TestCase):
    def user_store_patches(self, data_dir: str):
        root = Path(data_dir)
        return (
            patch.object(matter_store, "DATA_DIR", root),
            patch.object(matter_store, "MATTERS_PATH", root / "matters.json"),
            patch.object(matter_store, "UPLOADS_DIR", root / "uploads"),
        )

    def test_google_user_session_and_login_state_are_persisted_without_raw_tokens(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.user_store_patches(data_dir)
            with patches[0], patches[1], patches[2]:
                state = user_store.create_login_state(next_path="/api/matters")
                first_state = user_store.consume_login_state(state)
                second_state = user_store.consume_login_state(state)
                user = user_store.upsert_google_user({
                    "sub": "google-subject",
                    "email": "User@Example.com",
                    "name": "User Example",
                    "picture": "https://example.com/profile.png",
                })
                token = user_store.create_session(user["id"])
                session_user = user_store.user_for_session_token(token)
                users_payload = json.loads((Path(data_dir) / "users.json").read_text(encoding="utf-8"))

        self.assertEqual(first_state["next_path"], "/api/matters")
        self.assertEqual(first_state["metadata"], {})
        self.assertIsNone(second_state)
        self.assertEqual(user["id"], "google:google-subject")
        self.assertEqual(user["email"], "user@example.com")
        self.assertEqual(session_user["id"], user["id"])
        self.assertNotIn(token, json.dumps(users_payload))
        self.assertEqual(len(users_payload["sessions"]), 1)


if __name__ == "__main__":
    unittest.main()
