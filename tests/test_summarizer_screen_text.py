import unittest

from core.summarizer import (
    MAX_PROMPT_CHARS,
    _build_prompt,
    _screen_text_fingerprint,
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

    def test_fingerprint_ignores_chrome_lines(self):
        left = "Real page title about embeddings\nMinimize\nClose"
        right = "Real page title about embeddings\nMaximize\nRestore"
        self.assertEqual(_screen_text_fingerprint(left), _screen_text_fingerprint(right))


if __name__ == "__main__":
    unittest.main()
