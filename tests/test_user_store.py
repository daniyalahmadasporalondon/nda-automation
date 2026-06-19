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
            # The user store resolves NDA_USERS_PATH before falling back to
            # DATA_DIR/users.json, and the test harness (conftest) sets it to its
            # own tmp file. Pin it to this test's data_dir so patching DATA_DIR is
            # authoritative and the assertions below read the file we wrote.
            patch.dict("os.environ", {"NDA_USERS_PATH": str(root / "users.json")}),
        )

    def test_google_user_session_and_login_state_are_persisted_without_raw_tokens(self):
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.user_store_patches(data_dir)
            with patches[0], patches[1], patches[2], patches[3]:
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
                listed_users = user_store.list_users()
                sync_status = user_store.record_user_gmail_sync(
                    user["id"],
                    {
                        "imported": [{"id": "matter_1"}],
                        "query": "has:attachment",
                        "skipped": [
                            {"reason": "duplicate_attachment"},
                            {"reason": "review_failed"},
                        ],
                        "deduplicated_count": 2,
                    },
                    synced_at="2026-06-04T17:00:00+00:00",
                    started_at="2026-06-04T16:59:58+00:00",
                    finished_at="2026-06-04T17:00:00+00:00",
                )
                user_store.upsert_google_user({
                    "sub": "google-subject",
                    "email": "User@Example.com",
                    "name": "User Example",
                    "picture": "https://example.com/profile.png",
                })
                sync_after_login = user_store.gmail_sync_status(user["id"])
                users_payload = json.loads((Path(data_dir) / "users.json").read_text(encoding="utf-8"))

        self.assertEqual(first_state["next_path"], "/api/matters")
        self.assertEqual(first_state["metadata"], {})
        self.assertIsNone(second_state)
        self.assertEqual(user["id"], "google:google-subject")
        self.assertEqual(user["email"], "user@example.com")
        self.assertEqual(session_user["id"], user["id"])
        self.assertEqual([listed_user["id"] for listed_user in listed_users], [user["id"]])
        self.assertEqual(sync_status["last_sync_imported_count"], 1)
        self.assertEqual(sync_status["last_sync_skipped_count"], 2)
        self.assertEqual(sync_status["sync_history"][0]["duplicate_count"], 1)
        self.assertEqual(sync_status["sync_history"][0]["deduplicated_count"], 2)
        self.assertEqual(sync_status["sync_history"][0]["review_failed_count"], 1)
        self.assertEqual(sync_after_login, sync_status)
        self.assertNotIn(token, json.dumps(users_payload))
        self.assertEqual(len(users_payload["sessions"]), 1)

    def _seed_user(self, sub: str = "google-subject"):
        return user_store.upsert_google_user({
            "sub": sub,
            "email": f"{sub}@example.com",
            "name": "User Example",
            "picture": "https://example.com/profile.png",
        })

    def test_idle_timed_out_session_is_invalid(self):
        """A session untouched past the idle window is rejected even before its
        absolute TTL elapses (FIX #23 sliding inactivity timeout)."""
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.user_store_patches(data_dir)
            with patches[0], patches[1], patches[2], patches[3]:
                base = 1_000_000.0
                with patch.object(user_store, "_now_epoch", return_value=base):
                    user = self._seed_user()
                    token = user_store.create_session(user["id"])
                    # Immediately valid.
                    self.assertIsNotNone(user_store.user_for_session_token(token))
                # Jump past the idle window (but well within the 14-day TTL).
                idle_future = base + user_store.SESSION_IDLE_TTL_SECONDS + 60
                with patch.object(user_store, "_now_epoch", return_value=idle_future):
                    self.assertIsNone(user_store.user_for_session_token(token))

    def test_active_use_slides_idle_window_forward(self):
        """Touching a session refreshes last_seen_at so continuous use never
        idle-expires (keeps single-session login working)."""
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.user_store_patches(data_dir)
            with patches[0], patches[1], patches[2], patches[3]:
                base = 2_000_000.0
                with patch.object(user_store, "_now_epoch", return_value=base):
                    user = self._seed_user()
                    token = user_store.create_session(user["id"])
                # Use it just before the window closes -> slides forward.
                near_edge = base + user_store.SESSION_IDLE_TTL_SECONDS - 10
                with patch.object(user_store, "_now_epoch", return_value=near_edge):
                    self.assertIsNotNone(user_store.user_for_session_token(token))
                # A second window measured from the refreshed last_seen is still alive.
                still_alive = near_edge + user_store.SESSION_IDLE_TTL_SECONDS - 10
                with patch.object(user_store, "_now_epoch", return_value=still_alive):
                    self.assertIsNotNone(user_store.user_for_session_token(token))

    def test_logout_all_clears_every_session_for_user(self):
        """delete_all_sessions_for_user revokes all of a user's sessions while
        leaving other users untouched (FIX #23 log-out-everywhere)."""
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.user_store_patches(data_dir)
            with patches[0], patches[1], patches[2], patches[3]:
                user = self._seed_user("alice")
                other = self._seed_user("bob")
                tokens = [user_store.create_session(user["id"]) for _ in range(3)]
                other_token = user_store.create_session(other["id"])

                removed = user_store.delete_all_sessions_for_user(user["id"])

                self.assertEqual(removed, 3)
                for token in tokens:
                    self.assertIsNone(user_store.user_for_session_token(token))
                # Bob's session is unaffected.
                self.assertIsNotNone(user_store.user_for_session_token(other_token))

    def test_per_user_session_cap_evicts_oldest(self):
        """Logging in beyond MAX_SESSIONS_PER_USER evicts the oldest sessions so
        they cannot accumulate unbounded (FIX #23 session cap)."""
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.user_store_patches(data_dir)
            with patches[0], patches[1], patches[2], patches[3]:
                user = self._seed_user()
                tokens = []
                base = 3_000_000.0
                # Create one more than the cap, each a second apart so ordering
                # by last activity is deterministic.
                for i in range(user_store.MAX_SESSIONS_PER_USER + 1):
                    with patch.object(user_store, "_now_epoch", return_value=base + i):
                        tokens.append(user_store.create_session(user["id"]))
                # The very first (oldest) token is evicted; the rest survive.
                with patch.object(
                    user_store,
                    "_now_epoch",
                    return_value=base + user_store.MAX_SESSIONS_PER_USER + 1,
                ):
                    self.assertIsNone(user_store.user_for_session_token(tokens[0]))
                    for token in tokens[1:]:
                        self.assertIsNotNone(user_store.user_for_session_token(token))


    def test_odd_shaped_subtree_is_not_silently_emptied_on_disk(self):
        """REGRESSION: a structurally-odd sub-tree (e.g. ``login_states`` saved as
        a JSON list rather than an object) must NOT be silently coerced to ``{}``
        and persisted back, destroying the original blob. A read that prunes an
        expired session previously rewrote the coerced-empty store to disk.
        """
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.user_store_patches(data_dir)
            with patches[0], patches[1], patches[2], patches[3]:
                users_path = Path(data_dir) / "users.json"
                # An odd-but-parseable store: login_states is a LIST (the shape
                # surprise) while sessions is a normal dict holding ONE expired
                # session so a read/prune is provoked into wanting to persist.
                odd_store = {
                    "version": user_store.USER_STORE_VERSION,
                    "users": {},
                    "sessions": {
                        "deadhash": {
                            "user_id": "ghost",
                            "expires_at": "2000-01-01T00:00:00+00:00",
                            "created_at": "2000-01-01T00:00:00+00:00",
                        }
                    },
                    # Structural surprise that must survive on disk.
                    "login_states": ["unexpected", "list", "payload"],
                }
                users_path.write_text(json.dumps(odd_store), encoding="utf-8")

                # The integrity condition is surfaced, NOT silently coerced.
                with self.assertRaises(user_store.UserStoreError):
                    user_store.user_for_session_token("any-token")

                # Crucially, the original odd blob is intact on disk -- nothing
                # was overwritten with an empty object.
                on_disk = json.loads(users_path.read_text(encoding="utf-8"))
                self.assertEqual(on_disk["login_states"], ["unexpected", "list", "payload"])
                self.assertEqual(on_disk, odd_store)

    def test_normal_expired_session_prune_still_persists(self):
        """A well-shaped store with an expired session is pruned and the pruned
        result IS persisted (proves the integrity guard did not over-rotate into
        disabling legitimate prune-and-save)."""
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.user_store_patches(data_dir)
            with patches[0], patches[1], patches[2], patches[3]:
                users_path = Path(data_dir) / "users.json"
                store = {
                    "version": user_store.USER_STORE_VERSION,
                    "users": {},
                    "sessions": {
                        "expiredhash": {
                            "user_id": "ghost",
                            "expires_at": "2000-01-01T00:00:00+00:00",
                            "created_at": "2000-01-01T00:00:00+00:00",
                        },
                        "livehash": {
                            "user_id": "ghost",
                            "expires_at": "2999-01-01T00:00:00+00:00",
                            "created_at": "2999-01-01T00:00:00+00:00",
                            "last_seen_at": "2999-01-01T00:00:00+00:00",
                        },
                    },
                    "login_states": {},
                }
                users_path.write_text(json.dumps(store), encoding="utf-8")

                # Reading triggers the prune of the expired session.
                self.assertIsNone(user_store.user_for_session_token("any-token"))

                on_disk = json.loads(users_path.read_text(encoding="utf-8"))
                # Expired entry actually removed and persisted...
                self.assertNotIn("expiredhash", on_disk["sessions"])
                # ...while the still-live session survives.
                self.assertIn("livehash", on_disk["sessions"])


if __name__ == "__main__":
    unittest.main()
