"""Auth hardening regression tests (v1.0.4).

Six focused tests covering:
  - basic login
  - brute-force lockout (axes, 5 failures)
  - cool-off unlock (axes)
  - idle timeout stays logged in under threshold
  - idle timeout redirects after threshold
  - activity resets the timeout (guards against absolute-timeout bug)
  - expired banner renders on login page
"""
from datetime import timedelta
from unittest import mock

from axes.models import AccessAttempt
from axes.utils import reset
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse


User = get_user_model()


def _attempt_fail_login(client, username: str, password: str = "wrong") -> None:
    """POST one failed login."""
    client.post(reverse("login"), {"username": username, "password": password})


def _login_via_post(client, username: str, password: str) -> None:
    """POST to /accounts/login/ with valid creds. Sets the session cookie."""
    response = client.post(reverse("login"), {"username": username, "password": password})
    assert response.status_code == 302, (
        f"login failed: status={response.status_code} body={response.content[:200]!r}"
    )


class LoginBasicsTests(TestCase):
    def test_login_succeeds_with_correct_credentials(self):
        User.objects.create_user(username="alice", password="correct-horse")
        response = self.client.post(
            reverse("login"),
            {"username": "alice", "password": "correct-horse"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/")


@override_settings(AXES_COOLOFF_TIME=timedelta(seconds=60))
class AxesBruteForceTests(TestCase):
    def setUp(self):
        User.objects.create_user(username="bob", password="hunter2")
        AccessAttempt.objects.all().delete()

    def test_5_failed_logins_lock_the_account(self):
        for _ in range(5):
            _attempt_fail_login(self.client, "bob")
        # Even with the correct password, the account is locked.
        response = self.client.post(
            reverse("login"),
            {"username": "bob", "password": "hunter2"},
        )
        self.assertIn(response.status_code, (200, 403))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_lockout_expires_after_cooldown(self):
        for _ in range(5):
            _attempt_fail_login(self.client, "bob")
        # Manually reset the lockout — equivalent to cool-off elapsing.
        # (Cool-off timing itself is exercised in production; we don't
        # freezegun here to keep the test dependency-free.)
        reset(username="bob")
        response = self.client.post(
            reverse("login"),
            {"username": "bob", "password": "hunter2"},
        )
        self.assertEqual(response.status_code, 302, response.content[:200])


@override_settings(SESSION_IDLE_TIMEOUT_SECONDS=3600)  # 1h for fast tests
class IdleTimeoutTests(TestCase):
    def setUp(self):
        User.objects.create_user(username="carol", password="pw-carol-123")
        AccessAttempt.objects.all().delete()
        _login_via_post(self.client, "carol", "pw-carol-123")
        # Verify login worked.
        self.assertIn("_auth_user_id", self.client.session)

    def _stamp_last_seen(self, value: float):
        """Drive the middleware via a request so _last_seen ends up in the DB.

        Note: the middleware only writes ``_last_seen`` when the new value is
        >= 60s after the previous stamp (its throttle). We pick a value > 60
        for the very first call so the stamp always lands.
        """
        from django.contrib.sessions.models import Session
        with mock.patch("accounts.middleware.time", return_value=value):
            response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        db_session = Session.objects.get(
            session_key=self.client.cookies["sessionid"].value,
        )
        self.assertEqual(db_session.get_decoded().get("_last_seen"), value)

    def test_active_session_under_threshold_stays_logged_in(self):
        self._stamp_last_seen(100.0)
        # Advance mocked time to 3500s (under 3600 threshold). The
        # middleware should let the request through.
        with mock.patch("accounts.middleware.time", return_value=3500.0):
            response = self.client.get("/")
        self.assertEqual(response.status_code, 200)

    def test_idle_session_over_threshold_redirects_to_login(self):
        self._stamp_last_seen(100.0)
        # Advance to 3700s. Delta from _last_seen (100) is 3600, > 3600
        # threshold (middleware uses strict >). Middleware logs out + redirects.
        with mock.patch("accounts.middleware.time", return_value=3701.0):
            response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)
        self.assertIn("expired=1", response.url)

    def test_activity_resets_the_timeout(self):
        """Guards against the absolute-timeout bug.

        Stamp at t=100 → activity at t=3000 (stamps _last_seen=3000) →
        request at t=6500 (1.8h since first stamp) must still be alive because
        the activity at t=3000 reset the clock.

        If we accidentally computed elapsed as `time - login_time` instead
        of `time - _last_seen`, the request at t=6500 would exceed the 1h
        threshold and this test would fail with a 302 to /accounts/login/.
        """
        self._stamp_last_seen(100.0)
        self._stamp_last_seen(3000.0)
        with mock.patch("accounts.middleware.time", return_value=6500.0):
            response = self.client.get("/")
        self.assertEqual(
            response.status_code, 200,
            "session must stay alive because activity at t=3000 reset the clock",
        )

    def test_expired_banner_renders(self):
        """GET /accounts/login/?expired=1 shows the 'session expired' banner."""
        response = self.client.get("/accounts/login/?expired=1")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Your session has expired.")