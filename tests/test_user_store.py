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


    def test_valid_session_survives_save_failure_and_logs_skip(self):
        """A full disk (save raises UserStoreError) must NOT 500 a valid session:
        the last-seen bookkeeping save is fail-soft, so the user still resolves
        and a single structured skip line is logged (no PII/token)."""
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.user_store_patches(data_dir)
            with patches[0], patches[1], patches[2], patches[3]:
                base = 5_000_000.0
                with patch.object(user_store, "_now_epoch", return_value=base):
                    user = self._seed_user()
                    token = user_store.create_session(user["id"])
                # Move far enough past the throttle that the slide wants to persist,
                # then make that persist fail like a full disk would.
                touch_at = base + user_store.SESSION_LAST_SEEN_PERSIST_INTERVAL_SECONDS + 5
                disk_full = OSError(28, "No space left on device")
                save_error = user_store.UserStoreError("User store could not be saved.")
                save_error.__cause__ = disk_full
                with patch.object(user_store, "_now_epoch", return_value=touch_at), \
                        patch.object(user_store, "_save_store_unlocked", side_effect=save_error), \
                        patch("builtins.print") as mock_print:
                    resolved = user_store.user_for_session_token(token)

        # The valid session STILL returns its user despite the failed save.
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["id"], user["id"])
        # Exactly one structured skip line was logged, carrying the errno/class
        # but neither the token nor any PII.
        printed = [call.args[0] for call in mock_print.call_args_list if call.args]
        skip_lines = [line for line in printed if "user_store_save_skipped" in str(line)]
        self.assertEqual(len(skip_lines), 1)
        event = json.loads(skip_lines[0])
        self.assertEqual(event["event"], "user_store_save_skipped")
        self.assertEqual(event["context"], "session_touch")
        self.assertEqual(event["errno"], 28)
        self.assertEqual(event["error_class"], "UserStoreError")
        self.assertNotIn(token, skip_lines[0])

    def test_expired_session_returns_none_even_with_save_failure(self):
        """An idle-expired session is still rejected (returns None) even when the
        pruning save fails -- fail-soft must not resurrect a dead session."""
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.user_store_patches(data_dir)
            with patches[0], patches[1], patches[2], patches[3]:
                base = 6_000_000.0
                with patch.object(user_store, "_now_epoch", return_value=base):
                    user = self._seed_user()
                    token = user_store.create_session(user["id"])
                idle_future = base + user_store.SESSION_IDLE_TTL_SECONDS + 60
                with patch.object(user_store, "_now_epoch", return_value=idle_future), \
                        patch.object(
                            user_store,
                            "_save_store_unlocked",
                            side_effect=user_store.UserStoreError("full disk"),
                        ):
                    self.assertIsNone(user_store.user_for_session_token(token))

    def test_session_create_still_raises_on_save_failure(self):
        """Write-critical paths stay HARD-failing: a login/session-create must
        surface a save failure so the user knows their login didn't persist.
        This is deliberately NOT fail-soft."""
        with tempfile.TemporaryDirectory() as data_dir:
            patches = self.user_store_patches(data_dir)
            with patches[0], patches[1], patches[2], patches[3]:
                user = self._seed_user()
                with patch.object(
                    user_store,
                    "_save_store_unlocked",
                    side_effect=user_store.UserStoreError("full disk"),
                ):
                    with self.assertRaises(user_store.UserStoreError):
                        user_store.create_session(user["id"])


if __name__ == "__main__":
    unittest.main()
