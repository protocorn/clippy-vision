import unittest

from core.summarizer import (
    MAX_PROMPT_CHARS,
    _activity_fingerprint,
    _build_prompt,
    _model_skipped,
    _screen_text_fingerprint,
    is_contentful_for_summary,
    is_useful_screen_text,
)


class SummarizerScreenTextTests(unittest.TestCase):
    def test_ui_chrome_is_not_useful(self):
        chrome = (
            "1786722150920_processed.jpg - Clippy_Vision - Cursor\n"
            "Minimize\nMaximize\nRestore\nClose\nChrome Legacy Window"
        )
        self.assertFalse(is_useful_screen_text(chrome))

    def test_real_content_is_useful(self):
        text = (
            "news.ycombinator.com/item?id=4915683\n"
            "Prior Labs is hiring ML infrastructure engineers in Berlin.\n"
            "Deep learning skipped tables for clinical trials and financial models."
        )
        self.assertTrue(is_useful_screen_text(text))

    def test_prompt_dedupes_identical_screen_text(self):
        blob = (
            "package.json - Clippy_Vision\n"
            "name: clippy-vision\n"
            "version: 1.2.0\n"
            "local AI desktop companion with screen aware memory"
        )
        events = [
            {
                "summary": f"event {index}",
                "vision_activity": "",
                "vision_ocr_text": blob,
                "timestamp": float(index),
            }
            for index in range(5)
        ]
        prompt = _build_prompt(events)
        self.assertEqual(prompt.count(" | screen text: "), 1)
        self.assertLessEqual(len(prompt), MAX_PROMPT_CHARS)

    def test_prompt_caps_total_size(self):
        events = []
        for index in range(40):
            events.append(
                {
                    "summary": f"context switch {index} " + ("x" * 80),
                    "vision_activity": "reading docs",
                    "vision_ocr_text": (
                        f"unique document page {index}\n"
                        + ("meaningful content about APIs and databases " * 20)
                    ),
                    "timestamp": float(index),
                }
            )
        prompt = _build_prompt(events)
        self.assertLessEqual(len(prompt), MAX_PROMPT_CHARS)
        self.assertTrue(prompt.startswith("Events:\n"))

    def test_status_bar_screenshot_is_not_contentful(self):
        self.assertFalse(is_contentful_for_summary({
            "event_type": "screenshot_analysis",
            "process_name": "Cursor.exe",
            "window_title": "storage.py - Clippy_Vision - Cursor",
            "summary": "Background screenshot of Cursor.exe - storage.py - Clippy_Vision - Cursor",
            "vision_activity": "Cursor.exe - storage.py - Clippy_Vision - Cursor",
            "vision_ocr_text": "Minimize\nMaximize\nClose",
        }))

    def test_photos_with_text_is_contentful(self):
        self.assertTrue(is_contentful_for_summary({
            "event_type": "screenshot_analysis",
            "process_name": "Photos.exe",
            "window_title": "vacation.png - Photos",
            "summary": "Background screenshot of Photos.exe",
            "vision_ocr_text": "A labeled diagram of the checkout flow with three numbered steps.",
        }))

    def test_real_cursor_screen_is_contentful_and_title_is_in_the_prompt(self):
        event = {
            "timestamp": 10.0,
            "event_type": "screenshot_analysis",
            "process_name": "Cursor.exe",
            "window_title": "supero_launchway_one_pager.md - Job_Application_Agent - Cursor",
            "summary": "Background screenshot of the one pager",
            "vision_ocr_text": (
                "Launchway is a Python CLI and backend that tailors a resume "
                "to a job and fills the employer application form."
            ),
        }
        self.assertTrue(is_contentful_for_summary(event))
        prompt = _build_prompt([event])
        self.assertIn("window: supero_launchway_one_pager.md - Job_Application_Agent - Cursor", prompt)
        self.assertNotIn("window: Program Manager", prompt)

    def test_same_paste_and_title_share_a_fingerprint(self):
        def event(stamp: float) -> dict:
            return {
                "timestamp": stamp,
                "event_type": "paste",
                "process_name": "Cursor.exe",
                "window_title": "storage.py - Clippy_Vision - Cursor",
                "summary": "Pasted content: fix the thread local connection in storage.py",
            }
        self.assertEqual(_activity_fingerprint([event(1.0)]), _activity_fingerprint([event(2.0)]))

    def test_thin_model_summary_is_a_skip(self):
        self.assertTrue(_model_skipped({"skip": True, "summary": "looked at the desktop"}))
        self.assertTrue(_model_skipped({"skip": False, "summary": ""}))
        self.assertFalse(_model_skipped({
            "skip": False,
            "summary": "The user edited supero_launchway_one_pager.md in Cursor.",
        }))

    def test_fingerprint_ignores_chrome_lines(self):
        left = "Real page title about embeddings\nMinimize\nClose"
        right = "Real page title about embeddings\nMaximize\nRestore"
        self.assertEqual(_screen_text_fingerprint(left), _screen_text_fingerprint(right))


if __name__ == "__main__":
    unittest.main()
