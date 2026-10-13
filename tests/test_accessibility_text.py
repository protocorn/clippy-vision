"""Unit tests for structure-first accessibility extraction helpers."""

from __future__ import annotations

import builtins
import sys
import unittest

from unittest.mock import patch

import pytest
from PIL import Image

from core.accessibility_text import (
    _best_content_region,
    _best_region_text,
    _find_best_document,
    _region_score,
    _run_with_timeout,
    _text_from_control,
    is_useful_accessibility_text,
    looks_like_nav_soup,
    normalize_accessibility_text,
    prefer_active_text,
    rank_text_by_point,
    strip_ui_chrome,
)
from core.screen_tiles import choose_active_tile, discover_tiles, forget_windows, pick_winners
from core.secret_patterns import paint_secret_text


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

    def test_dense_short_lines_are_a_real_screen(self):
        screen = "\n".join(f"def helper_{index}(value): return value + {index}" for index in range(40))
        self.assertFalse(looks_like_nav_soup(screen))
        self.assertTrue(is_useful_accessibility_text(screen))

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


def _text(name: str, rect) -> _FakeControl:
    return _FakeControl(control_type="TextControl", content=True, name=name, rect=rect)


def _workbench(*, editor_lines=("def capture_screenshot(timestamp_ms: int) -> Path | None:",),
               terminal_lines=("PS C:\\Users\\proto> npm start", "[capture] Started")):
    """Sidebar, editor, terminal, and a status bar under full-window wrappers."""
    files = [
        _FakeControl(
            control_type="TreeItemControl",
            name=f"sidebar_file_{index}.py",
            content=True,
            rect=(8, 40 + index * 18, 200, 56 + index * 18),
        )
        for index in range(40)
    ]
    sidebar = _FakeControl(control_type="TreeControl", content=True, rect=(0, 0, 220, 872), children=files)
    editor = _FakeControl(
        control_type="EditControl",
        content=True,
        rect=(220, 0, 1400, 600),
        children=[_text(line, (400, 180 + index * 24, 1100, 200 + index * 24)) for index, line in enumerate(editor_lines)],
    )
    terminal = _FakeControl(
        control_type="GroupControl",
        content=True,
        rect=(220, 600, 1400, 872),
        children=[_text(line, (230, 620 + index * 20, 1300, 636 + index * 20)) for index, line in enumerate(terminal_lines)],
    )
    center = _FakeControl(control_type="GroupControl", content=True, rect=(220, 0, 1400, 872), children=[editor, terminal])
    body = _FakeControl(control_type="GroupControl", content=True, rect=(0, 0, 1400, 872), children=[sidebar, center])
    status = _FakeControl(
        control_type="StatusBarControl",
        rect=(0, 872, 1400, 900),
        children=[_text("Agent Stats: 17/73 (23%)", (1100, 876, 1300, 896))],
    )
    splitter = _FakeControl(control_type="GroupControl", rect=(218, 0, 222, 872))
    wrapper = _FakeControl(control_type="PaneControl", rect=(0, 0, 1400, 900), children=[splitter, body, status])
    document = _FakeControl(control_type="DocumentControl", content=True, rect=(0, 0, 1400, 900), children=[wrapper])
    return document, {"sidebar": sidebar, "editor": editor, "terminal": terminal, "status": status}


class TileTests(unittest.TestCase):
    def setUp(self):
        forget_windows()

    def test_wrappers_bars_and_splitters_are_not_tiles(self):
        document, parts = _workbench()
        tiles = discover_tiles(document)
        self.assertEqual([tile.bounds for tile in tiles], [(0, 0, 220, 872), (220, 0, 1400, 600), (220, 600, 1400, 872)])
        self.assertNotIn(parts["status"], [tile.control for tile in tiles])

    def test_single_pane_window_is_one_tile(self):
        editor = _FakeControl(control_type="EditControl", content=True, rect=(0, 0, 800, 600), text="Hello from Notepad")
        window = _FakeControl(control_type="WindowControl", rect=(0, 0, 800, 600), children=[editor])
        tiles = discover_tiles(window)
        self.assertEqual(len(tiles), 1)
        self.assertEqual(tiles[0].bounds, (0, 0, 800, 600))

    def test_caret_in_the_editor_keeps_the_other_panes(self):
        document, _parts = _workbench()
        choice = choose_active_tile(document, window="w", activity_box=(480, 190, 482, 208))
        self.assertIn("capture_screenshot", choice.text)
        self.assertIn("sidebar_file_0", choice.text)
        self.assertIn("npm start", choice.text)
        self.assertNotIn("Agent Stats", choice.text)
        self.assertEqual(choice.bounds, (220, 0, 1400, 600))
        self.assertLess(choice.text.find("capture_screenshot"), choice.text.find("sidebar_file_0"))

    def test_caret_in_the_sidebar_keeps_the_editor_too(self):
        document, _parts = _workbench()
        choice = choose_active_tile(document, window="w", activity_box=(40, 80, 42, 96))
        self.assertIn("sidebar_file_0.py", choice.text)
        self.assertIn("capture_screenshot", choice.text)
        self.assertLess(choice.text.find("sidebar_file_0"), choice.text.find("capture_screenshot"))

    def test_changed_text_wins_without_a_caret(self):
        first, _parts = _workbench()
        choose_active_tile(first, window="w")
        second, _parts = _workbench(terminal_lines=("PS C:\\Users\\proto> pytest", "101 passed in 23.4s"))
        choice = choose_active_tile(second, window="w")
        self.assertEqual(choice.bounds, (220, 600, 1400, 872))
        self.assertIn("101 passed", choice.text)
        self.assertIn("capture_screenshot", choice.text)
        self.assertLess(choice.text.find("101 passed"), choice.text.find("capture_screenshot"))

    def test_changed_lines_lead_the_stored_text(self):
        from core.screen_tiles import _line_id, order_stored_text

        previous = {_line_id("sidebar_file_0"), _line_id("npm start")}
        lines = ["sidebar_file_0", "npm start", "the new paragraph the user just typed"]
        stored = order_stored_text(lines, previous)
        self.assertTrue(stored.startswith("the new paragraph"))
        self.assertIn("sidebar_file_0", stored)

    def test_focus_decides_a_first_capture(self):
        document, parts = _workbench()
        choice = choose_active_tile(document, window="w", focused_bounds=(400, 180, 1100, 200))
        self.assertEqual(choice.winners[0].control, parts["editor"])

    def test_ties_keep_every_tied_tile(self):
        document, _parts = _workbench()
        tiles = discover_tiles(document)
        for tile in tiles:
            tile.score = 3.0
        tiles[0].score = 1.0
        winners = pick_winners(tiles)
        self.assertEqual([tile.bounds for tile in winners], [(220, 0, 1400, 600), (220, 600, 1400, 872)])

    def test_empty_editor_stays_empty_and_keeps_its_rectangle(self):
        document, _parts = _workbench(editor_lines=())
        choice = choose_active_tile(document, window="w", activity_box=(700, 400, 702, 418))
        self.assertIn("npm start", choice.text)
        self.assertNotIn("capture_screenshot", choice.text)
        self.assertEqual(choice.bounds, (220, 0, 1400, 600))

    def test_caret_text_survives_the_cap(self):
        active = "Email\nada@example.com\nFirst Name"
        filler = "\n".join(f"footer link {index} " + ("x" * 40) for index in range(200))
        ranked = prefer_active_text(active, filler)
        self.assertTrue(ranked.startswith("Email"))
        self.assertIn("ada@example.com", ranked)
        self.assertLessEqual(len(ranked), 4000)

    def test_nearest_snippet_is_stored_first(self):
        pieces = [
            ("Skip to Main Content and the page header", (0, 0, 400, 40)),
            ("Email ada@example.com", (200, 500, 700, 540)),
            ("Privacy Policy and social links in the footer", (0, 900, 400, 980)),
        ]
        ranked = rank_text_by_point(pieces, (400, 520))
        self.assertTrue(ranked.startswith("Email"))
        self.assertLess(ranked.find("Email"), ranked.find("Privacy Policy"))


class UiaThreadTests(unittest.TestCase):
    def test_helper_thread_initializes_ui_automation(self):
        import sys
        import threading

        entered = {}

        class _Init:
            def __enter__(self):
                entered["thread"] = threading.current_thread()
                return self

            def __exit__(self, *_args):
                return False

        with patch.dict(sys.modules, {"uiautomation": type(sys)("uiautomation")}):
            sys.modules["uiautomation"].UIAutomationInitializerInThread = _Init
            result = _run_with_timeout(lambda: "ready", 2.0)
        if sys.platform == "win32":
            self.assertEqual(result, "ready")
            self.assertIsNot(entered["thread"], threading.current_thread())
        else:
            self.assertEqual(result, "ready")
            self.assertNotIn("thread", entered)


class LateAccessibilityTests(unittest.TestCase):
    def test_result_after_the_timeout_is_delivered(self):
        import time

        def slow():
            time.sleep(0.3)
            return {"redacted_text": "editor text"}

        delivered = []
        result = _run_with_timeout(slow, 0.05, on_late=delivered.append)
        self.assertIsNone(result)
        deadline = time.time() + 2
        while not delivered and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(delivered, [{"redacted_text": "editor text"}])


class SecretPaintTests(unittest.TestCase):
    def test_useful_accessibility_skips_ocr(self):
        image = Image.new("RGB", (32, 32), (255, 255, 255))
        screen = (
            "Applications Engineer in Wyoming. Please fill out the required "
            "fields on this candidate profile before you submit."
        )
        with patch("core.ocr.ocr_line_boxes") as ocr:
            painted = paint_secret_text(image, screen)
        self.assertEqual(painted, 0)
        ocr.assert_not_called()

    def test_thin_accessibility_still_scans_pixels(self):
        image = Image.new("RGB", (32, 32), (255, 255, 255))
        with patch("core.ocr.ocr_line_boxes", return_value=[]) as ocr, patch(
            "core.app_settings.get_capture_settings",
            return_value={"ocr_enabled": True},
        ), patch("core.model_residency.can_run_ocr", return_value=True):
            painted = paint_secret_text(image, "Ok")
        self.assertEqual(painted, 0)
        ocr.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class TestLinuxAtSpiExtraction(unittest.TestCase):
    """Test Linux AT-SPI text extraction with mocked accessibility tree."""

    @pytest.mark.linux
    @patch("sys.platform", "linux")
    def test_atspi_extracts_text_from_active_app(self):
        """AT-SPI successfully extracts text from the active application."""
        from unittest.mock import MagicMock
        
        # Mock pyatspi module
        mock_pyatspi = MagicMock()
        mock_pyatspi.STATE_ACTIVE = 1
        mock_pyatspi.STATE_SENSITIVE = 2
        mock_pyatspi.ROLE_FRAME = 10
        mock_pyatspi.ROLE_TEXT = 20
        mock_pyatspi.ROLE_PUSH_BUTTON = 30
        mock_pyatspi.ROLE_PASSWORD_TEXT = 40
        mock_pyatspi.ROLE_MENU_BAR = 50
        
        # Mock accessible objects
        mock_text_accessible = MagicMock()
        mock_text_accessible.getRole.return_value = mock_pyatspi.ROLE_TEXT
        mock_text_accessible.getState.return_value = {mock_pyatspi.STATE_SENSITIVE}
        mock_text_accessible.childCount = 0
        mock_text_accessible.name = "Sample document text"
        
        mock_text_iface = MagicMock()
        mock_text_iface.characterCount = 50
        mock_text_iface.getText.return_value = "This is a document with some content"
        mock_text_accessible.queryText.return_value = mock_text_iface
        
        mock_app = MagicMock()
        mock_app.getRole.return_value = mock_pyatspi.ROLE_FRAME
        mock_app.getState.return_value = {mock_pyatspi.STATE_ACTIVE}
        mock_app.childCount = 1
        mock_app.getChildAtIndex.return_value = mock_text_accessible
        
        mock_desktop = MagicMock()
        mock_desktop.__iter__ = lambda self: iter([mock_app])
        
        mock_registry = MagicMock()
        mock_registry.getDesktop.return_value = mock_desktop
        mock_pyatspi.Registry = mock_registry
        
        with patch.dict("sys.modules", {"pyatspi": mock_pyatspi}):
            if "core.accessibility_text" in sys.modules:
                del sys.modules["core.accessibility_text"]
            
            from core import accessibility_text
            
            # Reset flags after import
            accessibility_text._linux_at_spi_warned = False
            accessibility_text._linux_at_spi_unavailable = False
            
            text = accessibility_text._linux_text()
            
            assert "document with some content" in text.lower()

    @pytest.mark.linux
    @patch("sys.platform", "linux")
    def test_atspi_handles_empty_tree_gracefully(self):
        """AT-SPI handles empty accessibility tree without crashing."""
        from unittest.mock import MagicMock
        
        mock_pyatspi = MagicMock()
        mock_pyatspi.STATE_ACTIVE = 1
        
        # Empty desktop (no applications)
        mock_desktop = MagicMock()
        mock_desktop.__iter__ = lambda self: iter([])
        
        mock_registry = MagicMock()
        mock_registry.getDesktop.return_value = mock_desktop
        mock_pyatspi.Registry = mock_registry
        
        with patch.dict("sys.modules", {"pyatspi": mock_pyatspi}):
            if "core.accessibility_text" in sys.modules:
                del sys.modules["core.accessibility_text"]
            
            from core import accessibility_text
            
            accessibility_text._linux_at_spi_warned = False
            accessibility_text._linux_at_spi_unavailable = False
            
            text = accessibility_text._linux_text()
            
            assert text == ""

    @pytest.mark.linux
    @patch("sys.platform", "linux")
    def test_atspi_skips_password_fields(self):
        """AT-SPI skips password fields and does not extract their values."""
        from unittest.mock import MagicMock
        
        mock_pyatspi = MagicMock()
        mock_pyatspi.STATE_ACTIVE = 1
        mock_pyatspi.STATE_SENSITIVE = 2
        mock_pyatspi.ROLE_FRAME = 10
        mock_pyatspi.ROLE_PASSWORD_TEXT = 40
        
        # Mock password field
        mock_password = MagicMock()
        mock_password.getRole.return_value = mock_pyatspi.ROLE_PASSWORD_TEXT
        mock_password.getState.return_value = {mock_pyatspi.STATE_SENSITIVE}
        mock_password.name = "password123"  # Should not be extracted
        mock_password.childCount = 0
        
        mock_app = MagicMock()
        mock_app.getRole.return_value = mock_pyatspi.ROLE_FRAME
        mock_app.getState.return_value = {mock_pyatspi.STATE_ACTIVE}
        mock_app.childCount = 1
        mock_app.getChildAtIndex.return_value = mock_password
        
        mock_desktop = MagicMock()
        mock_desktop.__iter__ = lambda self: iter([mock_app])
        
        mock_registry = MagicMock()
        mock_registry.getDesktop.return_value = mock_desktop
        mock_pyatspi.Registry = mock_registry
        
        with patch.dict("sys.modules", {"pyatspi": mock_pyatspi}):
            if "core.accessibility_text" in sys.modules:
                del sys.modules["core.accessibility_text"]
            
            from core import accessibility_text
            
            accessibility_text._linux_at_spi_warned = False
            accessibility_text._linux_at_spi_unavailable = False
            
            text = accessibility_text._linux_text()
            
            assert "password123" not in text

    @pytest.mark.linux
    @patch("sys.platform", "linux")
    def test_atspi_skips_insensitive_password_fields(self):
        """AT-SPI skips password fields even when they are insensitive/disabled."""
        from unittest.mock import MagicMock
        
        mock_pyatspi = MagicMock()
        mock_pyatspi.STATE_ACTIVE = 1
        mock_pyatspi.STATE_SENSITIVE = 2
        mock_pyatspi.ROLE_FRAME = 10
        mock_pyatspi.ROLE_PASSWORD_TEXT = 40
        
        # Mock insensitive/disabled password field (no STATE_SENSITIVE)
        mock_password = MagicMock()
        mock_password.getRole.return_value = mock_pyatspi.ROLE_PASSWORD_TEXT
        mock_password.getState.return_value = set()  # Empty state set, no SENSITIVE
        mock_password.name = "disabled_password_value"  # Should NOT be extracted
        mock_password.childCount = 0
        mock_password.queryText.side_effect = NotImplementedError
        mock_password.queryValue.side_effect = NotImplementedError
        
        mock_app = MagicMock()
        mock_app.getRole.return_value = mock_pyatspi.ROLE_FRAME
        mock_app.getState.return_value = {mock_pyatspi.STATE_ACTIVE}
        mock_app.childCount = 1
        mock_app.getChildAtIndex.return_value = mock_password
        mock_app.queryText.side_effect = NotImplementedError
        mock_app.queryValue.side_effect = NotImplementedError
        
        mock_desktop = MagicMock()
        mock_desktop.__iter__ = lambda self: iter([mock_app])
        
        mock_registry = MagicMock()
        mock_registry.getDesktop.return_value = mock_desktop
        mock_pyatspi.Registry = mock_registry
        
        with patch.dict("sys.modules", {"pyatspi": mock_pyatspi}):
            if "core.accessibility_text" in sys.modules:
                del sys.modules["core.accessibility_text"]
            
            from core import accessibility_text
            
            accessibility_text._linux_at_spi_warned = False
            accessibility_text._linux_at_spi_unavailable = False
            
            text = accessibility_text._linux_text()
            
            # Even insensitive password fields should be skipped
            assert "disabled_password_value" not in text

    @pytest.mark.linux
    @patch("sys.platform", "linux")
    def test_atspi_respects_wall_clock_timeout(self):
        """AT-SPI walk respects wall-clock timeout and returns partial results."""
        from unittest.mock import MagicMock
        import time
        
        mock_pyatspi = MagicMock()
        mock_pyatspi.STATE_ACTIVE = 1
        mock_pyatspi.STATE_SENSITIVE = 2
        mock_pyatspi.ROLE_FRAME = 10
        mock_pyatspi.ROLE_TEXT = 20
        
        # Mock fast accessible (returns immediately)
        mock_fast = MagicMock()
        mock_fast.getRole.return_value = mock_pyatspi.ROLE_TEXT
        mock_fast.getState.return_value = {mock_pyatspi.STATE_SENSITIVE}
        mock_fast.name = "Fast text"
        mock_fast.childCount = 0
        mock_fast.queryText.side_effect = NotImplementedError
        mock_fast.queryValue.side_effect = NotImplementedError
        
        # Mock slow accessible that simulates a hung app
        call_count = [0]
        def slow_get_role():
            call_count[0] += 1
            if call_count[0] > 1:
                # Simulate blocking on second call
                time.sleep(3.0)  # Longer than _ATSPI_WALK_TIMEOUT_SECONDS (2.0s)
            return mock_pyatspi.ROLE_TEXT
        
        mock_slow = MagicMock()
        mock_slow.getRole.side_effect = slow_get_role
        mock_slow.getState.return_value = {mock_pyatspi.STATE_SENSITIVE}
        mock_slow.name = "Slow text that should timeout"
        mock_slow.childCount = 0
        
        call_index = [0]
        def get_child(index):
            call_index[0] += 1
            if call_index[0] == 1:
                return mock_fast
            return mock_slow
        
        mock_app = MagicMock()
        mock_app.getRole.return_value = mock_pyatspi.ROLE_FRAME
        mock_app.getState.return_value = {mock_pyatspi.STATE_ACTIVE}
        mock_app.childCount = 2
        mock_app.getChildAtIndex.side_effect = get_child
        
        mock_desktop = MagicMock()
        mock_desktop.__iter__ = lambda self: iter([mock_app])
        
        mock_registry = MagicMock()
        mock_registry.getDesktop.return_value = mock_desktop
        mock_pyatspi.Registry = mock_registry
        
        with patch.dict("sys.modules", {"pyatspi": mock_pyatspi}):
            if "core.accessibility_text" in sys.modules:
                del sys.modules["core.accessibility_text"]
            
            from core import accessibility_text
            
            accessibility_text._linux_at_spi_warned = False
            accessibility_text._linux_at_spi_unavailable = False
            
            start = time.time()
            text = accessibility_text._linux_text()
            elapsed = time.time() - start
            
            # Should complete quickly (within ~2.5s), not wait for the full 3s sleep
            assert elapsed < 2.8, f"Walk took {elapsed}s, expected < 2.8s"
            
            # Should have collected the fast text before timing out
            assert "Fast text" in text
            # Should NOT have waited for slow text
            assert "Slow text" not in text

    @pytest.mark.linux
    @patch("sys.platform", "linux")
    def test_atspi_handles_dbus_error(self):
        """AT-SPI handles D-Bus connection errors gracefully."""
        from unittest.mock import MagicMock
        
        mock_pyatspi = MagicMock()
        mock_registry = MagicMock()
        mock_registry.getDesktop.side_effect = Exception("org.freedesktop.DBus.Error: Connection refused")
        mock_pyatspi.Registry = mock_registry
        
        with patch.dict("sys.modules", {"pyatspi": mock_pyatspi}):
            if "core.accessibility_text" in sys.modules:
                del sys.modules["core.accessibility_text"]
            
            from core import accessibility_text
            accessibility_text._linux_at_spi_warned = False
            accessibility_text._linux_at_spi_unavailable = False
            
            text = accessibility_text._linux_text()
            
            assert text == ""
            assert accessibility_text._linux_at_spi_warned is True

    @pytest.mark.linux
    @patch("sys.platform", "linux")
    def test_atspi_handles_timeout(self):
        """AT-SPI handles timeout errors gracefully."""
        from unittest.mock import MagicMock
        
        mock_pyatspi = MagicMock()
        mock_registry = MagicMock()
        mock_registry.getDesktop.side_effect = Exception("timeout waiting for reply")
        mock_pyatspi.Registry = mock_registry
        
        with patch.dict("sys.modules", {"pyatspi": mock_pyatspi}):
            if "core.accessibility_text" in sys.modules:
                del sys.modules["core.accessibility_text"]
            
            from core import accessibility_text
            accessibility_text._linux_at_spi_warned = False
            accessibility_text._linux_at_spi_unavailable = False
            
            text = accessibility_text._linux_text()
            
            assert text == ""
            assert accessibility_text._linux_at_spi_warned is True

    @pytest.mark.linux
    @patch("sys.platform", "linux")
    def test_atspi_handles_no_desktop(self):
        """AT-SPI handles missing desktop gracefully (accessibility disabled)."""
        from unittest.mock import MagicMock
        
        mock_pyatspi = MagicMock()
        mock_registry = MagicMock()
        mock_registry.getDesktop.return_value = None  # Accessibility disabled
        mock_pyatspi.Registry = mock_registry
        
        with patch.dict("sys.modules", {"pyatspi": mock_pyatspi}):
            if "core.accessibility_text" in sys.modules:
                del sys.modules["core.accessibility_text"]
            
            from core import accessibility_text
            accessibility_text._linux_at_spi_warned = False
            accessibility_text._linux_at_spi_unavailable = False
            
            text = accessibility_text._linux_text()
            
            assert text == ""
            # Should mark as unavailable to avoid retrying
            assert accessibility_text._linux_at_spi_unavailable is True

    @pytest.mark.linux
    @patch("sys.platform", "linux")
    def test_atspi_filters_chrome_roles(self):
        """AT-SPI filters out UI chrome elements (menu bar, toolbar, etc.)."""
        from unittest.mock import MagicMock
        
        mock_pyatspi = MagicMock()
        mock_pyatspi.STATE_ACTIVE = 1
        mock_pyatspi.STATE_SENSITIVE = 2
        mock_pyatspi.ROLE_FRAME = 10
        mock_pyatspi.ROLE_TEXT = 20
        mock_pyatspi.ROLE_MENU_BAR = 50
        mock_pyatspi.ROLE_TOOL_BAR = 51
        
        # Mock menu bar (should be filtered)
        mock_menubar = MagicMock()
        mock_menubar.getRole.return_value = mock_pyatspi.ROLE_MENU_BAR
        mock_menubar.getState.return_value = {mock_pyatspi.STATE_SENSITIVE}
        mock_menubar.name = "File Edit View"
        mock_menubar.childCount = 0
        mock_menubar.queryText.side_effect = NotImplementedError
        mock_menubar.queryValue.side_effect = NotImplementedError
        
        # Mock content text (should be kept)
        mock_text = MagicMock()
        mock_text.getRole.return_value = mock_pyatspi.ROLE_TEXT
        mock_text.getState.return_value = {mock_pyatspi.STATE_SENSITIVE}
        mock_text.name = "Actual page content"
        mock_text.childCount = 0
        mock_text.queryText.side_effect = NotImplementedError
        mock_text.queryValue.side_effect = NotImplementedError
        
        mock_app = MagicMock()
        mock_app.getRole.return_value = mock_pyatspi.ROLE_FRAME
        mock_app.getState.return_value = {mock_pyatspi.STATE_ACTIVE}
        mock_app.childCount = 2
        
        def get_child(index):
            if index == 0:
                return mock_menubar
            return mock_text
        
        mock_app.getChildAtIndex.side_effect = get_child
        
        mock_desktop = MagicMock()
        mock_desktop.__iter__ = lambda self: iter([mock_app])
        
        mock_registry = MagicMock()
        mock_registry.getDesktop.return_value = mock_desktop
        mock_pyatspi.Registry = mock_registry
        
        with patch.dict("sys.modules", {"pyatspi": mock_pyatspi}):
            if "core.accessibility_text" in sys.modules:
                del sys.modules["core.accessibility_text"]
            
            from core import accessibility_text
            
            accessibility_text._linux_at_spi_warned = False
            accessibility_text._linux_at_spi_unavailable = False
            
            text = accessibility_text._linux_text()
            
            assert "Actual page content" in text
            assert "File Edit View" not in text

    @pytest.mark.linux
    @patch("sys.platform", "linux")
    def test_accessibility_text_returns_empty_when_pyatspi_missing(self):
        """Accessibility text extraction returns empty string when pyatspi is not installed."""
        import sys as sys_module
        
        if "core.accessibility_text" in sys_module.modules:
            del sys_module.modules["core.accessibility_text"]
        
        # Save original __import__ function
        original_import = builtins.__import__
        
        def mock_import(name, *args, **kwargs):
            if name == "pyatspi":
                raise ImportError("No module named 'pyatspi'")
            return original_import(name, *args, **kwargs)
        
        with patch("builtins.__import__", side_effect=mock_import):
            from core import accessibility_text
            
            # Reset the warning flag for this test
            accessibility_text._linux_at_spi_warned = False
            accessibility_text._linux_at_spi_unavailable = False
            
            text = accessibility_text.extract_accessibility_text()
            
            # Should return empty, not crash
            assert text == ""
