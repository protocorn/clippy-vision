"""Private-window detection for Chrome, Edge, and Brave."""

import unittest

from core.private_windows import (
    bounds_overlap,
    chrome_control_is_private,
    chrome_root_name_is_private,
    chrome_window_is_private,
    control_is_private,
    forget_private_windows,
    mode_reply_is_private,
    parse_private_bounds,
    root_name_is_private,
)
from core.process_names import process_key


class _Node:
    def __init__(self, class_name, name="", children=None):
        self.ClassName = class_name
        self.Name = name
        self._children = children or []

    def GetChildren(self):
        return list(self._children)


class ChromePrivateNameTests(unittest.TestCase):
    def test_english_incognito_page_is_private(self):
        name = "THERE Definition & Meaning - Merriam-Webster - Google Chrome (Incognito)"
        self.assertTrue(chrome_root_name_is_private(name))

    def test_new_incognito_tab_is_private(self):
        self.assertTrue(chrome_root_name_is_private("New Incognito Tab - Google Chrome (Incognito)"))

    def test_another_language_uses_the_same_shape(self):
        self.assertTrue(chrome_root_name_is_private("Résultats - Google Chrome (Navigation privée)"))

    def test_beta_channel_is_private(self):
        self.assertTrue(chrome_root_name_is_private("New Tab - Google Chrome Beta (Incognito)"))

    def test_translated_channel_is_private(self):
        self.assertTrue(
            chrome_root_name_is_private("Résultats\u00a0–\u00a0Google Chrome\u00a0Bêta (Navigation privée)")
        )
        self.assertTrue(
            chrome_root_name_is_private(
                "Résultats\u00a0–\u00a0Google\u00a0Chrome pour les développeurs (Navigation privée)"
            )
        )

    def test_normal_profile_is_not_private(self):
        self.assertFalse(chrome_root_name_is_private("New Tab - Google Chrome - Alex"))

    def test_profile_with_parentheses_is_not_private(self):
        name = "Cursor - The best way to code with AI - Google Chrome - Alex (Work)"
        self.assertFalse(chrome_root_name_is_private(name))
        french = "Résultats - Google Chrome – Alex (Work)"
        self.assertFalse(chrome_root_name_is_private(french))

    def test_page_title_mentioning_chrome_is_not_private(self):
        name = "Deal - Google Chrome (Sale) - Google Chrome"
        self.assertFalse(chrome_root_name_is_private(name))

    def test_tree_reads_the_root_pane_and_ignores_the_page(self):
        window = _Node(
            "Chrome_WidgetWin_1",
            "THERE Definition & Meaning - Merriam-Webster - Google Chrome",
            [
                _Node("Intermediate D3D Window"),
                _Node(
                    "BrowserRootView",
                    "THERE Definition & Meaning - Merriam-Webster - Google Chrome (Incognito)",
                    [_Node("DocumentControl", "secret search result")],
                ),
            ],
        )
        self.assertTrue(chrome_control_is_private(window))

    def test_missing_pane_is_unknown(self):
        self.assertIsNone(chrome_control_is_private(_Node("Chrome_WidgetWin_1", children=[_Node("ToolbarView")])))

    def test_non_chrome_process_skips_the_walk(self):
        forget_private_windows()
        self.assertFalse(chrome_window_is_private(123, "Cursor.exe"))


class EdgePrivateNameTests(unittest.TestCase):
    def test_inprivate_page_is_private(self):
        name = "search - Search - Microsoft Edge (InPrivate)"
        self.assertTrue(root_name_is_private(name, "msedge.exe"))

    def test_new_inprivate_tab_is_private(self):
        self.assertTrue(root_name_is_private("New InPrivate tab - Microsoft Edge (InPrivate)", "msedge.exe"))

    def test_sleeping_tab_stays_private(self):
        name = "hello world - Search - Sleeping - Microsoft Edge (InPrivate)"
        self.assertTrue(root_name_is_private(name, "msedge.exe"))

    def test_normal_window_is_not_private(self):
        self.assertFalse(root_name_is_private("normal - Search - Microsoft Edge", "msedge.exe"))

    def test_profile_with_parentheses_is_not_private(self):
        name = "normal - Search - Microsoft Edge - Profile 1 (Work)"
        self.assertFalse(root_name_is_private(name, "msedge.exe"))

    def test_chrome_name_is_not_an_edge_window(self):
        self.assertFalse(root_name_is_private("New Tab - Google Chrome (Incognito)", "msedge.exe"))

    def test_tree_reads_the_root_pane(self):
        window = _Node(
            "Chrome_WidgetWin_1",
            "search - Search - [InPrivate] - Microsoft Edge",
            [
                _Node("Intermediate D3D Window"),
                _Node(
                    "BrowserRootView",
                    "search - Search - Microsoft Edge (InPrivate)",
                    [_Node("DocumentControl", "secret search result")],
                ),
            ],
        )
        self.assertTrue(control_is_private(window, "msedge.exe"))


class BravePrivateNameTests(unittest.TestCase):
    def test_private_page_is_private(self):
        name = "JavaScript Program To Print Hello World - Brave (Private)"
        self.assertTrue(root_name_is_private(name, "brave.exe"))

    def test_normal_search_is_not_private(self):
        name = "hello world python - Brave Search - Brave"
        self.assertFalse(root_name_is_private(name, "brave.exe"))

    def test_profile_with_parentheses_is_not_private(self):
        self.assertFalse(root_name_is_private("New Tab - Brave - Work (Office)", "brave.exe"))

    def test_tree_reads_brave_root_pane(self):
        window = _Node(
            "Chrome_WidgetWin_1",
            "JavaScript Program To Print Hello World - Brave",
            [
                _Node("Intermediate D3D Window"),
                _Node(
                    "BraveBrowserRootView",
                    "JavaScript Program To Print Hello World - Brave (Private)",
                ),
            ],
        )
        self.assertTrue(control_is_private(window, "brave.exe"))
        self.assertFalse(control_is_private(window, "chrome.exe"))
        self.assertTrue(
            root_name_is_private(
                "JavaScript Program To Print Hello World - Brave (Private)",
                "Brave Browser",
            )
        )


class ProcessNameTests(unittest.TestCase):
    def test_windows_file_and_mac_app_are_the_same(self):
        self.assertEqual(process_key("chrome.exe"), process_key("Google Chrome"))
        self.assertEqual(process_key("msedge.exe"), process_key("Microsoft Edge"))
        self.assertEqual(process_key("brave.exe"), process_key("Brave Browser"))
        self.assertNotEqual(process_key("chrome.exe"), process_key("Code.exe"))

    def test_mac_mode_reply(self):
        self.assertTrue(mode_reply_is_private("incognito\n"))
        self.assertTrue(mode_reply_is_private("inprivate"))
        self.assertFalse(mode_reply_is_private("normal"))

    def test_private_bounds_parse_and_overlap(self):
        rects = parse_private_bounds("10,20,400,500\nbad\n")
        self.assertEqual(rects, [(10, 20, 400, 500)])
        self.assertTrue(bounds_overlap((12, 22, 398, 498), rects[0]))
        self.assertFalse(bounds_overlap((800, 800, 900, 900), rects[0]))


if __name__ == "__main__":
    unittest.main()
