"""VS Code-family editors expose file text only when accessibility support is on."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.editor_accessibility import (
    app_dir_name,
    ensure_screen_reader_support,
    user_settings_path,
)


class EditorAccessibilityTests(unittest.TestCase):
    def test_known_editors_map_to_their_user_data_folder(self):
        self.assertEqual(app_dir_name("Cursor.exe"), "Cursor")
        self.assertEqual(app_dir_name("Code.exe"), "Code")
        self.assertEqual(app_dir_name("Code - Insiders.exe"), "Code - Insiders")
        self.assertEqual(app_dir_name("VSCodium.exe"), "VSCodium")
        self.assertEqual(app_dir_name("Windsurf.exe"), "Windsurf")
        self.assertIsNone(app_dir_name("chrome.exe"))

    def test_missing_app_directory_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(user_settings_path("Cursor.exe", root=Path(tmp)))
            self.assertFalse(ensure_screen_reader_support("Cursor.exe", root=Path(tmp)))

    def test_off_becomes_on_and_comments_stay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = root / "Cursor" / "User" / "settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text(
                "{\n"
                "    // editor\n"
                '    "editor.accessibilitySupport": "off",\n'
                '    "editor.fontSize": 14\n'
                "}\n",
                encoding="utf-8",
            )
            self.assertTrue(ensure_screen_reader_support("cursor.exe", root=root))
            text = settings.read_text(encoding="utf-8")
            self.assertIn('"editor.accessibilitySupport": "on"', text)
            self.assertIn("// editor", text)
            self.assertIn('"editor.fontSize": 14', text)
            self.assertFalse(ensure_screen_reader_support("cursor.exe", root=root))

    def test_auto_becomes_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = root / "Code" / "User" / "settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text('{ "editor.accessibilitySupport": "auto" }\n', encoding="utf-8")
            self.assertTrue(ensure_screen_reader_support("Code.exe", root=root))
            self.assertIn(
                '"editor.accessibilitySupport": "on"',
                settings.read_text(encoding="utf-8"),
            )

    def test_missing_key_is_added_to_user_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = root / "Windsurf" / "User" / "settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text('{\n    "editor.fontSize": 16\n}\n', encoding="utf-8")
            self.assertTrue(ensure_screen_reader_support("Windsurf", root=root))
            text = settings.read_text(encoding="utf-8")
            self.assertIn('"editor.fontSize": 16,', text)
            self.assertIn('"editor.accessibilitySupport": "on"', text)

    def test_commented_out_key_is_not_the_one_that_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = root / "Cursor" / "User" / "settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text(
                "{\n"
                '    // "editor.accessibilitySupport": "off",\n'
                '    "editor.accessibilitySupport": "off"\n'
                "}\n",
                encoding="utf-8",
            )
            ensure_screen_reader_support("Cursor.exe", root=root)
            lines = settings.read_text(encoding="utf-8").splitlines()
            self.assertIn('// "editor.accessibilitySupport": "off"', lines[1])
            self.assertIn('"editor.accessibilitySupport": "on"', lines[2])


if __name__ == "__main__":
    unittest.main()
