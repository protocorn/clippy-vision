"""XYZ skill — watch a URL in the background (HTTP) and/or foreground (screen).

X = source URL
Y = keywords/phrases (literal) and/or intent (LLM on page diff)
Z = alert callback (+ optional user-defined LLM action on match)

Background: Clippy HTTP-fetches the URL on a timer.
Foreground: Clippy reads the focused tab when its URL matches the rule.
  Optional F5 is allowed only when the user is Away, idle, and opted in.

Change detection uses a rolling baseline per rule+channel:
  - first observation seeds baseline (no alert)
  - later observations compare via content hash
  - intent LLM receives a unified diff (not two full pages)

Run:
  python skills/when_x_then_y.py
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field, fields
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

import trafilatura

try:
    from core.paths import get_data_dir
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from core.paths import get_data_dir


USER_AGENT = "ClippyVision-XYZ/0.1 (local; +https://github.com/protocorn/clippy-vision)"
STATE_FILE = get_data_dir() / "xyz_skill_state.json"
RULES_FILE = get_data_dir() / "xyz_skill_rules.json"
CONFIG_FILE = get_data_dir() / "xyz_skill_config.json"
AWAY_FILE = get_data_dir() / "xyz_away.json"
MODEL = "qwen3:8b"
LLM_CONTEXT_MAX_CHARS = 8000
LLM_SMALL_PAGE_CHARS = 4000
REFRESH_IDLE_SECONDS = 20.0
REFRESH_WAIT_SECONDS = 8.0
REFRESH_RELOAD_TIMEOUT = 14.0
FOREGROUND_IDLE_SECONDS = 8.0

INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "match": {"type": "boolean"},
        "reason": {"type": "string"},
        "snippet": {"type": "string"},
    },
    "required": ["match", "reason"],
}


@dataclass
class Rule:
    id: str
    mode: str  # background | foreground | both (v1: background only)
    source_url: str
    keywords: list[str] = field(default_factory=list)
    poll_seconds: int = 60
    enabled: bool = True
    cooldown_seconds: int = 300
    alert_message: str = "XYZ match: {title}"
    parser: str = "auto"

    require_all_keywords: bool = False  # True = AND, False = OR
    phrases: list[str] = field(default_factory=list)
    near: list[str] = field(default_factory=list)
    near_chars: int = 100
    intent: str = ""
    generator_prompt: str = ""  # optional; user text from UI, run as-is when a match fires
    allow_refresh: bool = False
    refresh_seconds: int = 120
    refresh_only_when_away: bool = True


@dataclass
class Match:
    rule_id: str
    item_id: str
    title: str
    url: str
    snippet: str
    matched_keywords: list[str] = field(default_factory=list)
    llm_reasoning: str = ""
    generator_response: str = ""


AlertFn = Callable[[Match, Rule], None] # callback function that takes a Match and a Rule and returns None


# ---------- persistence ----------

def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_rules() -> list[Rule]:
    raw = _load_json(RULES_FILE, [])
    allowed = {f.name for f in fields(Rule)}
    rules = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        try:
            rules.append(Rule(**{k: v for k, v in row.items() if k in allowed}))
        except TypeError:
            continue
    return rules


def save_rules(rules: list[Rule]) -> None:
    _save_json(RULES_FILE, [asdict(r) for r in rules])


def load_state() -> dict:
    return _load_json(
        STATE_FILE,
        {"baseline": {}, "last_alert_at": {}, "last_poll_at": {}, "last_refresh_at": {}},
    )


def save_state(state: dict) -> None:
    _save_json(STATE_FILE, state)


def load_config() -> dict:
    raw = _load_json(CONFIG_FILE, {})
    return {"enabled": bool(raw.get("enabled", False))}


def save_config(values: dict) -> dict:
    current = load_config()
    current.update(values or {})
    current["enabled"] = bool(current.get("enabled"))
    _save_json(CONFIG_FILE, current)
    return current


def is_xyz_enabled() -> bool:
    return load_config()["enabled"]


def get_away() -> bool:
    raw = _load_json(AWAY_FILE, {})
    return bool(raw.get("away", False))


def set_away(away: bool) -> dict:
    payload = {"away": bool(away), "updated_at": time.time()}
    _save_json(AWAY_FILE, payload)
    print(f"[XYZ] away={'on' if payload['away'] else 'off'}")
    return payload


def rule_is_valid(rule: Rule) -> bool:
    has_literal = bool(_clean_terms(rule.keywords) or _clean_terms(rule.phrases))
    has_intent = bool((rule.intent or "").strip())
    return bool((rule.source_url or "").strip()) and (has_literal or has_intent)


def has_literal_gate(rule: Rule) -> bool:
    return bool(_clean_terms(rule.keywords) or _clean_terms(rule.phrases))


def has_intent_gate(rule: Rule) -> bool:
    return bool((rule.intent or "").strip())


# ---------- fetch / extract ----------

class _PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._skip = False
        self.chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in {"script", "style", "noscript"}:
            self._skip = True
        elif t == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in {"script", "style", "noscript"}:
            self._skip = False
        elif t == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._skip or not data:
            return
        if self._in_title:
            self.title += data
        elif data.strip():
            self.chunks.append(data)


def fetch_page(url: str) -> tuple[str, str]:
    """Return (title, main_text). Prefer trafilatura; fall back to stdlib HTML."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        html = resp.read().decode("utf-8", errors="ignore")

    text = trafilatura.extract(
        html,
        url=url,
        include_comments=False,
        include_tables=False,
        favor_recall=True,
    )
    title = ""
    meta = trafilatura.extract_metadata(html, default_url=url)
    if meta is not None:
        title = (getattr(meta, "title", None) or "").strip()

    if text and text.strip():
        return title or url, text.strip()

    print("[XYZ] trafilatura miss; using HTML fallback")
    parser = _PageText()
    parser.feed(html)
    title = re.sub(r"\s+", " ", parser.title).strip() or url
    text = re.sub(r"\s+", " ", " ".join(parser.chunks)).strip()
    return title, text


def content_id(url: str, text: str) -> str:
    return hashlib.sha1(f"{url}|{text}".encode("utf-8", errors="ignore")).hexdigest()[:16]


def _baseline_key(rule_id: str, channel: str) -> str:
    return f"{rule_id}:{channel}"


def _get_baseline(state: dict, rule_id: str, channel: str = "background") -> dict | None:
    store = state.get("baseline", {})
    raw = store.get(_baseline_key(rule_id, channel))
    if isinstance(raw, dict):
        return raw
    if channel == "background" and isinstance(store.get(rule_id), dict):
        return store[rule_id]
    return None


def _set_baseline(
    state: dict,
    rule_id: str,
    *,
    item_id: str,
    title: str,
    text: str,
    channel: str = "background",
    evaluated: bool = True,
) -> None:
    state.setdefault("baseline", {})[_baseline_key(rule_id, channel)] = {
        "item_id": item_id,
        "title": title,
        "text": text,
        "evaluated": evaluated,
    }


def build_change_context(
    baseline_title: str,
    baseline_text: str,
    current_title: str,
    current_text: str,
    *,
    max_chars: int = LLM_CONTEXT_MAX_CHARS,
    small_page_chars: int = LLM_SMALL_PAGE_CHARS,
) -> str:
    """Build LLM context: full excerpts for small pages, unified diff otherwise."""
    combined = len(baseline_text) + len(current_text)
    if combined <= small_page_chars:
        half = max_chars // 2
        parts = [
            f"BASELINE TITLE: {baseline_title}",
            f"BASELINE:\n{baseline_text[:half]}",
            f"CURRENT TITLE: {current_title}",
            f"CURRENT:\n{current_text[:half]}",
        ]
        return "\n\n".join(parts)[:max_chars]

    diff_lines = difflib.unified_diff(
        baseline_text.splitlines(),
        current_text.splitlines(),
        fromfile="baseline",
        tofile="current",
        lineterm="",
    )
    diff = "\n".join(diff_lines)
    if not diff.strip():
        diff = "(Content hash changed but no line-level diff — possible formatting shift.)"

    header = f"TITLE CHANGE: {baseline_title!r} -> {current_title!r}\n\n"
    body = header + "CHANGES (unified diff):\n" + diff
    if len(body) > max_chars:
        body = body[: max_chars - 20] + "\n...(truncated)"
    return body


def url_matches(rule_url: str, active_url: str | None) -> bool:
    """True if the focused address matches the rule URL (host + path prefix)."""
    if not active_url or not rule_url:
        return False

    def _parts(raw: str) -> tuple[str, str]:
        text = raw.strip()
        if not text:
            return "", ""
        if "://" not in text:
            text = "https://" + text
        parsed = urllib.parse.urlparse(text)
        host = (parsed.netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        path = (parsed.path or "/").rstrip("/") or "/"
        return host, path

    rule_host, rule_path = _parts(rule_url)
    active_host, active_path = _parts(active_url)
    if not rule_host or rule_host != active_host:
        return False
    return active_path == rule_path or active_path.startswith(rule_path.rstrip("/") + "/")


def idle_seconds() -> float:
    """Seconds since last keyboard/mouse input. Huge value if unknown."""
    try:
        from core.platform_support import IS_WINDOWS
    except ImportError:
        IS_WINDOWS = False
    if IS_WINDOWS:
        import ctypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            millis = ctypes.windll.kernel32.GetTickCount() - info.dwTime
            return max(0.0, millis / 1000.0)
    return 10**9


def send_refresh() -> bool:
    """Send F5 to the foreground window. Returns False if unsupported."""
    try:
        from core.platform_support import IS_MACOS, IS_WINDOWS, _run_command
    except ImportError:
        return False
    try:
        if IS_WINDOWS:
            import win32api
            import win32con

            win32api.keybd_event(win32con.VK_F5, 0, 0, 0)
            win32api.keybd_event(win32con.VK_F5, 0, win32con.KEYEVENTF_KEYUP, 0)
            return True
        if IS_MACOS:
            _run_command(
                ["osascript", "-e", 'tell application "System Events" to key code 96'],
                timeout=2.0,
            )
            return True
    except Exception as exc:
        print(f"[XYZ] refresh failed: {exc}")
    return False


def fetch_foreground_page() -> tuple[str, str, str | None]:
    """Return (title, screen_text, active_url) for the focused window."""
    try:
        from core.accessibility_text import extract_accessibility_text
        from core.platform_support import get_window_metadata
        from core.privacy_settings import is_clippy_window, should_redact_window
    except ImportError:
        return "", "", None

    metadata = get_window_metadata()
    if not metadata:
        return "", "", None
    process_name = metadata.get("process_name") or ""
    title = metadata.get("current_window_title") or ""
    url = metadata.get("active_url")
    if is_clippy_window(process_name, title) or should_redact_window(process_name, title):
        return title, "", url
    text = extract_accessibility_text() or ""
    return title, text.strip(), url


# ---------- Y: literal gate ----------

def _clean_terms(terms: list[str] | None) -> list[str]:
    return [t for t in (terms or []) if (t or "").strip()]


def _word_pattern(term: str) -> re.Pattern:
    """Word-boundary match; multi-word terms become phrase patterns."""
    parts = [re.escape(p) for p in term.strip().lower().split() if p]
    if not parts:
        return re.compile(r"(?!)")
    inner = r"(?:\s+)".join(parts)
    return re.compile(rf"\b{inner}\b", re.IGNORECASE)


def find_hits(text: str, terms: list[str]) -> list[tuple[str, int]]:
    hits = []
    for term in terms:
        term = (term or "").strip()
        if not term:
            continue
        m = _word_pattern(term).search(text)
        if m:
            hits.append((term, m.start()))
    return hits


def proximity_ok(
    keyword_hits: list[tuple[str, int]],
    near: list[str],
    window: int,
    text: str,
) -> bool:
    if not near:
        return True
    near_hits = find_hits(text, near)
    if not near_hits:
        return False
    for _, ki in keyword_hits:
        for _, ni in near_hits:
            if abs(ki - ni) <= window:
                return True
    return False


def cheap_match(text: str, rule: Rule) -> list[str]:
    """Deterministic literal gate: keywords, phrases, proximity."""
    terms = _clean_terms(rule.keywords)
    phrases = _clean_terms(rule.phrases)

    keyword_hits = find_hits(text, terms) if terms else []
    phrase_hits = find_hits(text, phrases) if phrases else []

    if terms:
        hit_terms = [t for t, _ in keyword_hits]
        if rule.require_all_keywords:
            if len(hit_terms) < len({t.strip().lower() for t in terms}):
                return []
        elif not hit_terms:
            return []
    else:
        hit_terms = []

    if phrases:
        found_phrases = {t.lower() for t, _ in phrase_hits}
        needed = {p.strip().lower() for p in phrases}
        if not needed.issubset(found_phrases):
            return []
        hit_terms = hit_terms + [p for p in phrases if p.strip().lower() in found_phrases]

    anchor = keyword_hits or phrase_hits
    if not proximity_ok(anchor, _clean_terms(rule.near), rule.near_chars, text):
        return []

    out, seen = [], set()
    for t in hit_terms:
        key = t.lower()
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out


# ---------- Y: intent LLM ----------

def llm_intent_match(
    rule: Rule,
    baseline_title: str,
    baseline_text: str,
    current_title: str,
    current_text: str,
    *,
    page_scan: bool = False,
) -> tuple[bool, str, str]:
    """Return (matched, reason, snippet). Page scan or baseline→current diff."""
    try:
        from core.llm_gateway import Priority, gateway
    except ImportError:
        print("[XYZ] llm_gateway unavailable; skipping intent check")
        return False, "llm unavailable", ""

    change_context = build_change_context(
        baseline_title, baseline_text, current_title, current_text
    )
    if page_scan:
        system = (
            "You judge whether the CURRENT webpage content matches a user's watch intent.\n"
            "This is the first look at the page, not a diff. Return match=true if the page\n"
            "itself satisfies the intent.\n"
            "snippet: short quote (<=240 chars) from the page that justifies the decision."
        )
        user = f"INTENT:\n{rule.intent.strip()}\n\nCURRENT TITLE: {current_title}\n\nCURRENT PAGE:\n{current_text[:LLM_CONTEXT_MAX_CHARS]}"
    else:
        system = (
            "You judge whether a webpage CHANGE matches a user's watch intent.\n"
            "You receive a diff from a previous snapshot (baseline) to the current snapshot.\n"
            "Return match=true only if the CHANGE satisfies the intent — not merely because\n"
            "related words exist somewhere on the current page.\n"
            "snippet: short quote (<=240 chars) from the change that justifies the decision."
        )
        user = f"INTENT:\n{rule.intent.strip()}\n\n{change_context}"

    body = gateway.chat(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        model=MODEL,
        format=INTENT_SCHEMA,
        think=False,
        options={"temperature": 0},
        priority=Priority.BACKGROUND,
        keep_alive="10m",
        timeout=120,
    )
    if not body:
        return False, "empty llm response", ""

    content = body["message"]["content"]
    verdict = json.loads(content) if isinstance(content, str) else content
    return (
        bool(verdict.get("match")),
        str(verdict.get("reason") or ""),
        str(verdict.get("snippet") or "")[:300],
    )


def llm_generate(
    rule: Rule,
    title: str,
    url: str,
    snippet: str,
    *,
    change_context: str = "",
    current_text: str = "",
) -> str:
    """Run the user's prompt verbatim; page fields are context only."""
    prompt = (rule.generator_prompt or "").strip()
    if not prompt:
        return ""
    try:
        from core.llm_gateway import Priority, gateway
    except ImportError:
        return ""

    context_block = change_context.strip() or current_text[:4000]
    body = gateway.chat(
        messages=[
            {
                "role": "user",
                "content": (
                    f"{prompt}\n\n"
                    "---\n"
                    "Matched page context:\n"
                    f"Title: {title}\n"
                    f"URL: {url}\n"
                    f"Snippet: {snippet}\n\n"
                    f"{context_block}"
                ),
            },
        ],
        model=MODEL,
        think=False,
        options={"temperature": 0.3},
        priority=Priority.BACKGROUND,
        keep_alive="10m",
        timeout=120,
    )
    if not body:
        return ""
    return (body["message"]["content"] or "").strip()


# ---------- Z: alert ----------

def default_alert(match: Match, rule: Rule) -> None:
    print(f"[XYZ ALERT] {rule.alert_message.format(title=match.title, url=match.url)}")
    print(f"  url      : {match.url}")
    if match.matched_keywords:
        print(f"  keywords : {', '.join(match.matched_keywords)}")
    if match.llm_reasoning:
        print(f"  intent   : {match.llm_reasoning}")
    if match.snippet:
        print(f"  snippet  : {match.snippet[:240]}")
    if match.generator_response:
        print(f"  output   : {match.generator_response}")


def evaluate_snapshot(
    rule: Rule,
    state: dict,
    *,
    title: str,
    text: str,
    url: str,
    channel: str,
    alert: AlertFn = default_alert,
) -> None:
    """Compare a page snapshot to the rolling baseline and maybe alert."""
    if not rule.enabled or not rule_is_valid(rule):
        return

    blob = f"{title}\n{text}"
    item_id = content_id(url, text)
    last_alert_at = state.setdefault("last_alert_at", {})
    baseline = _get_baseline(state, rule.id, channel)
    first_eval = baseline is None or not baseline.get("evaluated")
    same_content = baseline is not None and baseline.get("item_id") == item_id

    if same_content and not first_eval:
        print(f"[XYZ] no change for {rule.id} ({channel})")
        return

    # Background HTTP: seed the first fetch, then alert on later diffs.
    if first_eval and channel != "foreground":
        _set_baseline(
            state, rule.id, item_id=item_id, title=title, text=text, channel=channel
        )
        print(f"[XYZ] seeded {channel} baseline for {rule.id}")
        save_state(state)
        return

    page_scan = first_eval
    if page_scan:
        print(f"[XYZ] judging current {channel} page for {rule.id}")
        baseline_title, baseline_text = "", ""
    else:
        baseline_title = str(baseline.get("title") or "")
        baseline_text = str(baseline.get("text") or "")
    change_context = build_change_context(baseline_title, baseline_text, title, text)

    def advance_baseline() -> None:
        _set_baseline(state, rule.id, item_id=item_id, title=title, text=text, channel=channel)
        save_state(state)

    hits: list[str] = []
    if has_literal_gate(rule):
        hits = cheap_match(blob, rule)
        if not hits:
            print(f"[XYZ] {rule.id} ({channel}) literal gate miss")
            advance_baseline()
            return

    llm_reason = ""
    snippet = text[:300]
    if has_intent_gate(rule):
        print(f"[XYZ] intent check for {rule.id} ({channel}, {'page' if page_scan else 'diff'})")
        ok, llm_reason, llm_snip = llm_intent_match(
            rule,
            baseline_title,
            baseline_text,
            title,
            text,
            page_scan=page_scan,
        )
        if llm_snip:
            snippet = llm_snip
        if not ok:
            print(f"[XYZ] intent rejected for {rule.id} ({channel}): {llm_reason}")
            retryable = "unavailable" in llm_reason or "empty" in llm_reason
            if not retryable:
                advance_baseline()
            return

    now = time.time()
    if now - float(last_alert_at.get(rule.id) or 0) < rule.cooldown_seconds:
        print(f"[XYZ] match on {rule.id} but cooldown active")
        advance_baseline()
        return

    generated = llm_generate(
        rule,
        title,
        url,
        snippet,
        change_context=change_context,
        current_text=text,
    )

    alert(
        Match(
            rule_id=rule.id,
            item_id=item_id,
            title=title,
            url=url,
            snippet=snippet,
            matched_keywords=hits,
            llm_reasoning=llm_reason,
            generator_response=generated,
        ),
        rule,
    )
    last_alert_at[rule.id] = now
    advance_baseline()


def check_rule(rule: Rule, state: dict, alert: AlertFn = default_alert) -> None:
    if not rule.enabled:
        return
    if not rule_is_valid(rule):
        print(f"[XYZ] skipping invalid rule {rule.id!r} (need keywords/phrases or intent)")
        return
    if rule.mode not in {"background", "both"}:
        return

    title, text = fetch_page(rule.source_url)
    evaluate_snapshot(
        rule,
        state,
        title=title,
        text=text,
        url=rule.source_url,
        channel="background",
        alert=alert,
    )


def _maybe_refresh(rule: Rule, state: dict, active_url: str | None) -> bool:
    """Reload the focused tab if the user opted in and is away + idle."""
    if not rule.allow_refresh:
        return False
    if rule.refresh_only_when_away and not get_away():
        return False
    if idle_seconds() < REFRESH_IDLE_SECONDS:
        print(f"[XYZ] skip refresh for {rule.id}: user not idle")
        return False
    if not url_matches(rule.source_url, active_url):
        return False

    last = float(state.setdefault("last_refresh_at", {}).get(rule.id) or 0)
    if time.time() - last < max(30, rule.refresh_seconds):
        return False
    if not send_refresh():
        return False
    state["last_refresh_at"][rule.id] = time.time()
    save_state(state)
    print(f"[XYZ] refreshed foreground tab for {rule.id}")
    return True


def _wait_for_reload(
    prev_id: str, prev_url: str | None
) -> tuple[str, str, str | None]:
    """Re-read the focused tab until screen text changes or we time out."""
    time.sleep(REFRESH_WAIT_SECONDS)
    last = fetch_foreground_page()
    deadline = time.time() + max(0.0, REFRESH_RELOAD_TIMEOUT - REFRESH_WAIT_SECONDS)
    while time.time() < deadline:
        title, text, url = last
        if text.strip() and content_id(url or prev_url or "", text) != prev_id:
            return last
        time.sleep(2.0)
        last = fetch_foreground_page()
    title, text, url = last
    if text.strip() and content_id(url or prev_url or "", text) == prev_id:
        print("[XYZ] refresh produced no new screen text")
    return last


def check_foreground_rule(rule: Rule, state: dict, alert: AlertFn = default_alert) -> None:
    if not rule.enabled or not rule_is_valid(rule):
        return
    if rule.mode not in {"foreground", "both"}:
        return

    title, text, active_url = fetch_foreground_page()
    if not url_matches(rule.source_url, active_url):
        if get_away():
            print(f"[XYZ] skip foreground {rule.id}: focused URL is {active_url or '(none)'}")
        return

    away = get_away()
    if not away and idle_seconds() < FOREGROUND_IDLE_SECONDS:
        print(f"[XYZ] skip foreground {rule.id}: user is active")
        return

    pre_id = content_id(active_url or "", text)
    if _maybe_refresh(rule, state, active_url):
        title, text, active_url = _wait_for_reload(pre_id, active_url)
    if not url_matches(rule.source_url, active_url):
        print(f"[XYZ] skip foreground {rule.id}: URL changed during refresh")
        return
    if not text.strip():
        print(f"[XYZ] skip foreground {rule.id}: no screen text")
        return

    evaluate_snapshot(
        rule,
        state,
        title=title,
        text=text,
        url=active_url or rule.source_url,
        channel="foreground",
        alert=alert,
    )


def run_once() -> None:
    state = load_state()
    for rule in load_rules():
        if rule.mode in {"background", "both"}:
            check_rule(rule, state)
        if rule.mode in {"foreground", "both"}:
            check_foreground_rule(rule, state)


def _due(state: dict, rule: Rule, now: float) -> bool:
    last = float(state.setdefault("last_poll_at", {}).get(rule.id) or 0)
    return now - last >= max(15, rule.poll_seconds)


def run_forever(*, seed_demo: bool = False) -> None:
    rules = load_rules()
    print(f"[XYZ] watching {len(rules)} rule(s)")
    for rule in rules:
        print(f"  - {rule.id} [{rule.mode}]: {rule.source_url} every {rule.poll_seconds}s")
    while True:
        if not seed_demo and not is_xyz_enabled():
            time.sleep(5)
            continue
        state = load_state()
        now = time.time()
        current = load_rules() or rules
        for rule in current:
            if not rule.enabled:
                continue
            try:
                if rule.mode in {"background", "both"} and _due(state, rule, now):
                    check_rule(rule, state)
                    state.setdefault("last_poll_at", {})[rule.id] = now
                    save_state(state)
                if rule.mode in {"foreground", "both"}:
                    check_foreground_rule(rule, state)
            except Exception as e:
                print(f"[XYZ] error on {rule.id}: {e}")
        sleep_for = max(15, min((r.poll_seconds for r in current if r.enabled), default=60))
        time.sleep(sleep_for)


def start_xyz_worker() -> None:
    import threading

    threading.Thread(target=run_forever, daemon=True, name="xyz-skill").start()
    print("[XYZ] worker started")


if __name__ == "__main__":
    # run_once()
    run_forever(seed_demo=True)
