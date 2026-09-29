"""Mac field records use the same paint rules as Windows UI Automation."""

import unittest

from core.mac_ui import parse_field_dump, parse_window_dump, redaction_from_nodes


class MacRedactionTests(unittest.TestCase):
    def test_password_field_is_painted_and_its_value_is_not_kept(self):
        result = redaction_from_nodes(
            [
                {
                    "role": "AXSecureTextField",
                    "name": "Password",
                    "value": "should-not-be-stored",
                    "bounds": (10, 20, 210, 48),
                }
            ]
        )
        self.assertEqual(result["edit_rects"], [(10, 20, 210, 48)])
        self.assertNotIn("should-not-be-stored", result["redacted_text"])

    def test_address_bar_is_left_visible(self):
        result = redaction_from_nodes(
            [
                {
                    "role": "AXTextField",
                    "name": "Address and search bar",
                    "value": "https://example.com",
                    "bounds": (0, 0, 800, 40),
                }
            ]
        )
        self.assertEqual(result["edit_rects"], [])

    def test_secret_in_a_text_area_is_painted_and_scrubbed(self):
        secret = "sk-" + ("a" * 24)
        result = redaction_from_nodes(
            [
                {
                    "role": "AXTextArea",
                    "name": "",
                    "value": f"token = {secret}",
                    "bounds": (0, 40, 600, 400),
                }
            ]
        )
        self.assertIn((0, 40, 600, 400), result["secret_rects"])
        self.assertNotIn(secret, result["redacted_text"])
        self.assertIn("[secret]", result["redacted_text"])

    def test_window_dump_becomes_screen_rectangles(self):
        rows = parse_window_dump("WhatsApp\tChat\t10\t20\t300\t400\n")
        self.assertEqual(rows[0]["process_name"], "WhatsApp")
        self.assertEqual(rows[0]["title"], "Chat")
        self.assertEqual(rows[0]["bounds"], (10, 20, 310, 420))

    def test_secure_field_dump_drops_the_value(self):
        rows = parse_field_dump("AXSecureTextField\tPassword\thunter2\t4\t8\t100\t24\n")
        self.assertEqual(rows[0]["value"], "")
        self.assertEqual(rows[0]["bounds"], (4, 8, 104, 32))


if __name__ == "__main__":
    unittest.main()
