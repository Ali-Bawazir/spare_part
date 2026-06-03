"""Tests for accounts.utils (role display name polish)."""

from django.test import SimpleTestCase

from accounts.utils import role_display_name, user_role_display


class RoleDisplayNameTests(SimpleTestCase):
    def test_known_role_codes_map_to_friendly_names(self):
        self.assertEqual(role_display_name("procurement"), "Maintenance Supply Officer")
        self.assertEqual(role_display_name("manager"), "Maintenance Manager")
        self.assertEqual(role_display_name("technician"), "Technician")
        self.assertEqual(role_display_name("supervisor"), "Supervisor")
        self.assertEqual(role_display_name("operator"), "Operator")
        self.assertEqual(role_display_name("super_admin"), "Super Admin")

    def test_unknown_role_code_returns_unchanged(self):
        self.assertEqual(role_display_name("something_weird"), "something_weird")

    def test_empty_role_returns_empty(self):
        self.assertEqual(role_display_name(""), "")
        self.assertEqual(role_display_name(None), "")

    def test_user_role_display_uses_role_attr(self):
        class _U:
            role = "procurement"
        self.assertEqual(user_role_display(_U()), "Maintenance Supply Officer")

    def test_user_role_display_handles_none(self):
        self.assertEqual(user_role_display(None), "")
