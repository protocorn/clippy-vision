"""Unit tests for structure-first accessibility extraction helpers."""

from __future__ import annotations

import unittest

from core.accessibility_text import (
    _best_content_region,
    _best_region_text,
    _find_best_document,
    _region_score,
    _text_from_control,
    is_useful_accessibility_text,
    looks_like_nav_soup,
    normalize_accessibility_text,
    strip_ui_chrome,
)


class _FakeRect:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class _FakeTextRange:
    def __init__(self, text: str):
        self._text = text

    def GetText(self, _limit: int) -> str:
        return self._text


class _FakeTextPattern:
    def __init__(self, text: str):
        self.DocumentRange = _FakeTextRange(text)


class _FakeControl:
    def __init__(
        self,
        *,
        control_type: str,
        name: str = "",
        area: int = 0,
        content: bool = False,
        text: str = "",
        children=None,
        rect=None,
    ):
        self.ControlTypeName = control_type
        self.Name = name
        self.IsContentElement = content
        self.IsPassword = False
        self._text = text
        self._children = children or []
        if rect is not None:
            self.BoundingRectangle = _FakeRect(*rect)
        else:
            # Encode area as a square for BoundingRectangle.
            side = int(area ** 0.5) if area else 0
            self.BoundingRectangle = _FakeRect(0, 0, side, side)

    def GetChildren(self):
        return list(self._children)

    def GetTextPattern(self):
        if not self._text:
            return None
        return _FakeTextPattern(self._text)

    def GetValuePattern(self):
        raise AttributeError("no value")


class AccessibilityStructureTests(unittest.TestCase):
    def test_chrome_lines_stripped(self):
        noise = "Minimize\nMaximize\nRestore\nClose\nChrome Legacy Window\nBack\nReload"
        self.assertEqual(strip_ui_chrome(noise), "")
        self.assertFalse(is_useful_accessibility_text(noise))

    def test_normalize_keeps_real_content(self):
        text = normalize_accessibility_text(
            "Minimize",
            "Back",
            "How We Build Effective Agents: Barry Zhang, Anthropic",
            "Close",
        )
        self.assertEqual(text, "How We Build Effective Agents: Barry Zhang, Anthropic")
        self.assertTrue(is_useful_accessibility_text(text))

    def test_find_best_document_prefers_content_document(self):
        chrome_doc = _FakeControl(
            control_type="DocumentControl",
            area=100_000,
            content=False,
            text="",
        )
        page_doc = _FakeControl(
            control_type="DocumentControl",
            area=90_000,
            content=True,
            text="Skip to content\nCrafting Interview Introduction\nWrite a strong opening paragraph",
        )
        toolbar = _FakeControl(control_type="ToolBarControl", name="Toolbar")
        root = _FakeControl(
            control_type="WindowControl",
            children=[toolbar, chrome_doc, page_doc],
        )
        best = _find_best_document(root)
        self.assertIs(best, page_doc)
        extracted = _text_from_control(best)
        self.assertIn("Crafting Interview Introduction", extracted)
        self.assertNotIn("Minimize", extracted)

    def test_nav_soup_is_not_useful(self):
        sidebar = "\n".join(
            [
                "New chat",
                "Pin conversation",
                "Open conversation options",
                "Pin conversation",
                "Open conversation options",
                "Pin conversation",
                "Open conversation options",
                "Pin conversation",
            ]
        )
        self.assertTrue(looks_like_nav_soup(sidebar))
        self.assertFalse(is_useful_accessibility_text(sidebar))

    def test_editor_stub_is_not_useful(self):
        stub = (
            "The editor is not accessible at this time. "
            "To enable screen reader optimized mode, use Shift+Alt+F1"
        )
        self.assertFalse(is_useful_accessibility_text(stub))

    def test_region_score_prefers_prose_over_short_labels(self):
        sidebar = "\n".join(["Pin conversation"] * 20)
        main = (
            "Here is a longer answer about how agents should use tools carefully, "
            "including when to fall back to OCR instead of trusting the accessibility tree."
        )
        # Same area: prose should still beat nav-label soup.
        self.assertGreater(_region_score(main, 200_000), _region_score(sidebar, 200_000))

    def test_best_region_picks_main_pane_over_sidebar(self):
        sidebar_items = [
            _FakeControl(control_type="ListItemControl", name=label, content=True, area=2_000)
            for label in (
                ["Pin conversation", "Open conversation options"] * 8
            )
        ]
        sidebar = _FakeControl(
            control_type="PaneControl",
            content=True,
            area=80_000,
            rect=(0, 0, 200, 400),
            children=[
                _FakeControl(
                    control_type="ListControl",
                    content=True,
                    area=70_000,
                    children=sidebar_items,
                )
            ],
        )
        main = _FakeControl(
            control_type="PaneControl",
            content=True,
            area=220_000,
            rect=(200, 0, 900, 400),
            children=[
                _FakeControl(
                    control_type="TextControl",
                    content=True,
                    name=(
                        "Crafting Interview Introduction. Write a strong opening "
                        "paragraph that explains your background and goals clearly "
                        "so the interviewer understands your motivation."
                    ),
                    area=200_000,
                )
            ],
        )
        document = _FakeControl(
            control_type="DocumentControl",
            content=True,
            area=320_000,
            children=[sidebar, main],
        )
        winner = _best_region_text(document)
        self.assertIn("Crafting Interview Introduction", winner)
        self.assertNotIn("Pin conversation", winner)

    def test_best_content_region_uses_area_and_central_position(self):
        sidebar = _FakeControl(
            control_type="PaneControl",
            content=True,
            rect=(0, 0, 180, 600),
            text="Pin conversation\nOpen conversation options",
        )
        main = _FakeControl(
            control_type="PaneControl",
            content=True,
            rect=(180, 60, 940, 560),
            text="",
        )
        scope = _FakeControl(
            control_type="DocumentControl",
            content=True,
            rect=(0, 0, 1000, 600),
            children=[sidebar, main],
        )
        self.assertIs(_best_content_region(scope), main)


if __name__ == "__main__":
    unittest.main()
