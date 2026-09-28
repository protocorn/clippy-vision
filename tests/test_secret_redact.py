"""Secret blackout: form-field memory and deterministic text patterns."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("CLIPPY_DATA_DIR", tempfile.mkdtemp(prefix="clippy-secret-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image

from core.secret_fields import (
    clear_field_memory,
    note_window_fields,
    paint_remembered_fields,
    remembered_rects,
    should_redact_edit,
)
from core.secret_patterns import (
    auth_page_label,
    label_kind,
    line_indexes_to_redact,
    line_is_secret,
    paired_row_secrets,
    redact_field_values,
    redact_secrets,
)

WINDOW = "chrome.exe\x1fSign in\x1fhttps://example.com/login"


def _nodes(revealed: bool) -> list[dict]:
    password_name = "" if revealed else "Password"
    return [
        {"name": "Email", "bounds": (100, 140, 400, 170), "is_password": False, "is_edit": False},
        {
            "name": password_name,
            "bounds": (100, 200, 400, 232),
            "is_password": not revealed,
            "is_edit": True,
        },
        {"name": "Sign in", "bounds": (100, 250, 220, 284), "is_password": False, "is_edit": False},
    ]


class FieldMemoryTests(unittest.TestCase):
    def setUp(self):
        clear_field_memory()

    def test_revealed_password_keeps_the_same_rectangle(self):
        note_window_fields(WINDOW, _nodes(revealed=False))
        masked = remembered_rects(WINDOW)
        note_window_fields(WINDOW, _nodes(revealed=True))
        self.assertEqual(len(masked), 1)
        self.assertEqual(len(remembered_rects(WINDOW)), 1)

    def test_failed_walk_does_not_forget_the_field(self):
        note_window_fields(WINDOW, _nodes(revealed=False))
        note_window_fields(WINDOW, None)
        self.assertEqual(len(remembered_rects(WINDOW)), 1)

    def test_other_window_is_not_painted_from_this_fingerprint(self):
        note_window_fields(WINDOW, _nodes(revealed=False))
        self.assertEqual(remembered_rects("notepad.exe\x1fNotes\x1f"), [])

    def test_revealed_field_is_caught_on_the_first_look(self):
        note_window_fields(
            WINDOW,
            [
                {"name": "Email", "bounds": (100, 140, 400, 168), "is_password": False, "is_edit": False},
                {"name": "Password", "bounds": (100, 172, 220, 196), "is_password": False, "is_edit": False},
                {"name": "", "bounds": (100, 200, 400, 232), "is_password": False, "is_edit": True},
                {"name": "Sign in", "bounds": (100, 250, 220, 284), "is_password": False, "is_edit": False},
            ],
        )
        self.assertEqual(remembered_rects(WINDOW), [(100, 200, 400, 232)])

    def test_search_box_is_not_a_secret_field(self):
        note_window_fields(
            WINDOW,
            [
                {
                    "name": "Search",
                    "bounds": (100, 200, 400, 232),
                    "is_password": False,
                    "is_edit": True,
                }
            ],
        )
        self.assertEqual(remembered_rects(WINDOW), [])

    def test_paint_blacks_the_field_and_leaves_the_rest(self):
        note_window_fields(WINDOW, _nodes(revealed=False))
        img = Image.new("RGB", (500, 400), (255, 0, 0))
        painted = paint_remembered_fields(
            img,
            {"left": 0, "top": 0, "width": 500, "height": 400},
            WINDOW,
        )
        self.assertEqual(painted, 1)
        self.assertEqual(img.getpixel((150, 210)), (0, 0, 0))
        self.assertEqual(img.getpixel((10, 10)), (255, 0, 0))


class PatternTests(unittest.TestCase):
    def test_prefixed_secrets_and_assignments(self):
        self.assertTrue(line_is_secret("ghp_" + "a" * 36))
        self.assertTrue(line_is_secret("AKIAIOSFODNN7EXAMPLE"))
        self.assertTrue(line_is_secret("-----BEGIN RSA PRIVATE KEY-----"))
        self.assertTrue(line_is_secret("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dBjftJeZ4CVP"))
        self.assertTrue(line_is_secret("password=Summer2024!"))
        self.assertTrue(line_is_secret("4242424242424242"))
        self.assertTrue(line_is_secret("GB82WEST12345698765432"))

    def test_ordinary_text_is_kept(self):
        self.assertFalse(line_is_secret("I forgot my password yesterday"))
        self.assertFalse(line_is_secret("password=true"))
        self.assertFalse(line_is_secret("order 12345"))
        self.assertIsNone(label_kind("I forgot my password yesterday"))
        self.assertEqual(label_kind("Enter your password"), "password")

    def test_env_assignment_and_url_userinfo_are_redacted(self):
        key = "AIza" + "x" * 32
        redacted = redact_secrets(f"GOOGLE_API_KEY={key}\nDEV_SETTINGS=use_database")
        self.assertNotIn(key, redacted)
        self.assertIn("GOOGLE_API_KEY=[secret]", redacted)
        self.assertIn("use_database", redacted)
        self.assertIn("your_api_key_here", redact_secrets("THEMUSE_API_KEY=your_api_key_here"))
        url = "redis://default:sup3r-secret-value@example.upstash.io:6379"
        self.assertNotIn("sup3r-secret-value", redact_secrets(f"REDIS_URL={url}"))

    def test_auth_page_is_the_path_not_a_query_or_a_substring(self):
        self.assertEqual(auth_page_label("https://www.mimikree.com/login"), "Sign-in page")
        self.assertEqual(auth_page_label("https://example.com/forgot-password"), "Password reset")
        self.assertIsNone(auth_page_label("https://example.com/docs/authentication-guide"))
        self.assertIsNone(auth_page_label("https://example.com/app?next=/login"))

    def test_single_line_edits_are_redacted_without_a_password_caption(self):
        self.assertTrue(should_redact_edit("", 36))
        self.assertFalse(should_redact_edit("Address and search bar", 36))
        self.assertFalse(should_redact_edit("", 800))

    def test_a_bare_password_word_is_not_a_text_secret(self):
        indexes = line_indexes_to_redact(["Email", "ada@example.com", "Password", "Summer2024!"])
        self.assertEqual(indexes, set())

    def test_a_token_on_the_same_row_as_a_secret_name_is_redacted(self):
        token = "Oiect" + "A" * 30
        other = "AQ." + "b" * 40
        nodes = [
            {"text": "ENCRYPTION_KEY", "bounds": (100, 200, 280, 228)},
            {"text": token, "bounds": (420, 200, 900, 228)},
            {"text": "DB_PORT", "bounds": (100, 240, 280, 268)},
            {"text": "5432", "bounds": (420, 240, 480, 268)},
            {"text": "DB_PASSWORD", "bounds": (100, 280, 280, 308)},
            {"text": "*******", "bounds": (420, 280, 520, 308)},
            {"text": "Set the API_KEY before deploy", "bounds": (100, 360, 700, 388)},
            {"text": other, "bounds": (100, 420, 700, 448)},
        ]
        paired = [item["text"] for item in paired_row_secrets(nodes)]
        self.assertEqual(paired, [token])
        stored = redact_field_values("\n".join(item["text"] for item in nodes), paired)
        self.assertNotIn(token, stored)
        self.assertIn("ENCRYPTION_KEY", stored)
        self.assertIn(other, stored)
