"""Deterministic secret patterns for text that is about to be saved.

The catalog follows Gitleaks' default rules (prefixed keys, private keys,
JWTs, assignment context). Path-only rules are omitted because a screenshot
has no file path. Generic high-entropy strings are included only when they
sit next to a secret label, which is what keeps source hashes on screen.

Source: https://github.com/gitleaks/gitleaks/blob/master/config/gitleaks.toml
"""

from __future__ import annotations

import math
import re
from collections import Counter

from PIL import Image, ImageDraw

# Short field captions. A line or control name matches only when the caption
# is the whole text, so a paragraph that mentions "password" is left alone.
_FIELD_LABELS = (
    "password",
    "passwd",
    "passcode",
    "passphrase",
    "pin",
    "cvv",
    "cvc",
    "security code",
    "card number",
    "account number",
    "routing number",
    "ssn",
    "social security number",
    "one time code",
    "one time password",
    "otp",
    "verification code",
    "api key",
    "access token",
    "secret key",
    "client secret",
)

_STOPWORDS = {
    "password",
    "passwd",
    "secret",
    "token",
    "changeme",
    "example",
    "placeholder",
    "redacted",
    "your_api_key",
    "your-api-key",
    "insert",
    "todo",
    "none",
    "null",
    "true",
    "false",
    "xxx",
    "xxxx",
}

# Prefixed credentials. Each rule is named after the Gitleaks rule it tracks.
_PREFIX_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key", re.compile(r"-----BEGIN[ A-Z0-9_-]{0,80}PRIVATE KEY")),
    ("private-key-end", re.compile(r"-----END[ A-Z0-9_-]{0,80}PRIVATE KEY")),
    (
        "jwt",
        re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ),
    ("aws-access-token", re.compile(r"(?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}")),
    ("github-pat", re.compile(r"ghp_[A-Za-z0-9]{36}")),
    ("github-fine-grained-pat", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("gitlab-pat", re.compile(r"glpat-[A-Za-z0-9\-_]{16,}")),
    ("slack-token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("stripe-access-token", re.compile(r"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("openai-api-key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{20,}")),
    ("anthropic-api-key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("google-api-key", re.compile(r"AIza[0-9A-Za-z_\-]{30,}")),
    ("npm-access-token", re.compile(r"npm_[A-Za-z0-9]{30,}")),
    ("sendgrid-api-token", re.compile(r"SG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}")),
    ("bearer-token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]{16,}={0,2}")),
)

# Names like GOOGLE_API_KEY and DB_PASSWORD. The underscore is a word
# character, so a pattern that requires a word boundary before "api" misses
# them. Match the suffix of the identifier instead.
_SECRET_NAME_SUFFIX = r"API_KEY|SECRET|PASSWORD|PASSWD|TOKEN|CREDENTIAL|_KEY"
_SECRET_NAME = re.compile(
    rf"(?i)^(?:[A-Za-z_][A-Za-z0-9_]*)?(?:{_SECRET_NAME_SUFFIX})$"
)
_ENV_ASSIGN = re.compile(
    rf"(?i)\b((?:[A-Za-z_][A-Za-z0-9_]*)?(?:{_SECRET_NAME_SUFFIX}))"
    r"\s*=\s*['\"]?([^\s'\"]+)"
)
_MASK_CHARS = set("*•●·")
_ROW_VALUE_MAX_PX = 140
_ROW_GAP_MAX_PX = 720
_USERINFO = re.compile(
    r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/@:]+:([^\s/@:]{4,})@"
)
_GOOGLE_CLIENT = re.compile(r"\b\d{6,}-[a-z0-9]{10,}\.apps\.googleusercontent\.com\b")
_PREFIX_RULES = _PREFIX_RULES + (
    ("google-oauth-client-secret", re.compile(r"\bGOCSPX-[A-Za-z0-9_\-]{20,}")),
    ("resend-api-key", re.compile(r"\bre_[A-Za-z0-9_]{20,}")),
)

_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_IBAN = re.compile(r"\b([A-Z]{2}\d{2}[A-Z0-9]{11,30})\b")
_LABEL_SPLIT = re.compile(r"[^a-z0-9]+")


def normalize_label(text: str) -> str:
    return " ".join(_LABEL_SPLIT.split((text or "").casefold())).strip()


def label_kind(text: str) -> str | None:
    """Return the field caption when `text` is that caption, not a sentence."""
    norm = normalize_label(text)
    if not norm or len(norm) > 40 or len(norm.split()) > 5:
        return None
    for label in _FIELD_LABELS:
        if norm == label or norm.endswith(" " + label):
            return label
    return None


def _shannon(value: str) -> float:
    counts = Counter(value)
    total = len(value)
    return -sum((count / total) * math.log2(count / total) for count in counts.values())


def _luhn_ok(digits: str) -> bool:
    total = 0
    double = False
    for char in reversed(digits):
        digit = int(char)
        if double:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
        double = not double
    return total % 10 == 0


def _iban_ok(value: str) -> bool:
    rearranged = value[4:] + value[:4]
    number = "".join(str(int(char, 36)) for char in rearranged)
    remainder = 0
    for char in number:
        remainder = (remainder * 10 + int(char)) % 97
    return remainder == 1


def _placeholder(value: str) -> bool:
    text = (value or "").strip().strip("'\"")
    low = text.casefold()
    if len(text) < 8 or low in _STOPWORDS:
        return True
    return low.startswith("your_") or low.endswith("_here")


def secret_values(text: str) -> list[str]:
    """Secret substrings in ``text``, in order, without placeholders.

    These are the spans a document buffer actually contains: prefixed keys,
    ``NAME=value`` assignments, and passwords embedded in URLs.
    """
    raw = text or ""
    found: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        value = (value or "").strip().strip("'\"")
        if not value or value in seen or _placeholder(value) or len(value) > 180:
            return
        seen.add(value)
        found.append(value)

    for _name, pattern in _PREFIX_RULES:
        for match in pattern.finditer(raw):
            add(match.group(0))
    for match in _ENV_ASSIGN.finditer(raw):
        add(match.group(2))
    for match in _USERINFO.finditer(raw):
        add(match.group(1))
    for match in _GOOGLE_CLIENT.finditer(raw):
        add(match.group(0))
    for match in _CARD.finditer(raw):
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            add(match.group(0))
    for match in _IBAN.finditer(raw):
        if _iban_ok(match.group(1)):
            add(match.group(1))
    return found


def redact_secrets(text: str) -> str:
    """Replace secret substrings with ``[secret]``. Names of assignments stay."""
    if not text:
        return text or ""
    redacted = text
    for value in secret_values(text):
        redacted = redacted.replace(value, "[secret]")
    return redacted


def redact_field_values(text: str, values: list[str]) -> str:
    """Remove the current contents of single-line fields from captured text."""
    cleaned = text or ""
    for value in values:
        token = (value or "").strip()
        if len(token) >= 4:
            cleaned = cleaned.replace(token, "[field]")
    return cleaned


def is_secret_name(text: str) -> bool:
    """True when the whole string is an identifier such as ``GEMINI_API_KEY``."""
    token = (text or "").strip()
    if not token or any(char.isspace() for char in token):
        return False
    return _SECRET_NAME.fullmatch(token) is not None


def is_single_token_value(text: str) -> bool:
    """A cell whose entire text is one token, not a mask or a placeholder."""
    token = (text or "").strip().strip("'\"")
    if not token or any(char.isspace() for char in token):
        return False
    if set(token) <= _MASK_CHARS:
        return False
    return not _placeholder(token)


def _same_row(name_box: tuple[int, int, int, int], value_box: tuple[int, int, int, int]) -> bool:
    """True when the two rectangles sit on one row, close enough to be a pair."""
    name_h = name_box[3] - name_box[1]
    value_h = value_box[3] - value_box[1]
    if name_h > _ROW_VALUE_MAX_PX or value_h > _ROW_VALUE_MAX_PX:
        return False
    shorter = min(name_h, value_h)
    overlap = min(name_box[3], value_box[3]) - max(name_box[1], value_box[1])
    if shorter < 8 or overlap < shorter * 0.5:
        return False
    if name_box == value_box:
        return True
    gap = max(0, max(name_box[0], value_box[0]) - min(name_box[2], value_box[2]))
    return gap <= _ROW_GAP_MAX_PX


def paired_row_secrets(nodes: list[dict]) -> list[dict]:
    """Value cells that sit on the same row as a secret-shaped name.

    Each node is ``{"text", "bounds"}`` with bounds ``(left, top, right, bottom)``.
    A sentence that mentions a key name has spaces, so it is not a name.
    Masked dots and short placeholders are not values. The returned nodes are
    the value cells to paint and to strip from stored text.
    """
    names: list[dict] = []
    values: list[dict] = []
    for node in nodes:
        text = str(node.get("text") or "").strip()
        bounds = node.get("bounds")
        if not text or not bounds or len(bounds) != 4:
            continue
        item = {"text": text, "bounds": tuple(int(v) for v in bounds)}
        if is_secret_name(text):
            names.append(item)
        elif is_single_token_value(text):
            values.append(item)
    used: set[int] = set()
    pairs: list[dict] = []
    for value in values:
        best: int | None = None
        best_gap: int | None = None
        for index, name in enumerate(names):
            if index in used or not _same_row(name["bounds"], value["bounds"]):
                continue
            gap = abs(value["bounds"][0] - name["bounds"][2])
            if best_gap is None or gap < best_gap:
                best = index
                best_gap = gap
        if best is None:
            continue
        used.add(best)
        pairs.append(value)
    return pairs


def line_is_secret(text: str) -> bool:
    """True when this OCR line or control value should be painted over."""
    return bool(secret_values(text))


def line_indexes_to_redact(lines: list[str]) -> set[int]:
    """OCR line indexes whose text contains a secret span."""
    return {index for index, line in enumerate(lines) if line_is_secret(line)}


_AUTH_SEGMENTS = {
    "login": "Sign-in page",
    "log-in": "Sign-in page",
    "signin": "Sign-in page",
    "sign-in": "Sign-in page",
    "logon": "Sign-in page",
    "auth": "Sign-in page",
    "oauth": "Sign-in page",
    "sso": "Sign-in page",
    "signup": "Sign-up page",
    "sign-up": "Sign-up page",
    "register": "Sign-up page",
    "forgot-password": "Password reset",
    "forgotpassword": "Password reset",
    "reset-password": "Password reset",
    "resetpassword": "Password reset",
}


def auth_page_label(url: str) -> str | None:
    """One-line description when the host or path is an auth page.

    Query parameters are ignored. ``?next=/login`` is not an auth page, and
    ``author`` does not match ``auth``.
    """
    from urllib.parse import urlparse

    raw = (url or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    segments = [part.casefold() for part in (parsed.hostname or "").split(".") if part]
    segments.extend(part.casefold() for part in parsed.path.split("/") if part)
    for segment in segments:
        label = _AUTH_SEGMENTS.get(segment)
        if label:
            return label
    return None


def _axis_box(points) -> tuple[int, int, int, int] | None:
    xs: list[float] = []
    ys: list[float] = []
    for point in points or []:
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            xs.append(float(point[0]))
            ys.append(float(point[1]))
    if not xs or not ys:
        return None
    return int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))


def paint_secret_text(img: Image.Image, screen_text: str = "") -> int:
    """Black out secret lines on the in-memory frame before it is saved.

    Password fields and secret spans already found in the accessibility tree
    are painted by the caller. This pass OCRs the pixels only when that text
    is too thin to trust (a canvas, a video, a window the tree could not read).
    It is also skipped when OCR is disabled or the machine is under pressure.
    """
    from core.accessibility_text import is_useful_accessibility_text

    if is_useful_accessibility_text(screen_text):
        from core.performance_metrics import increment

        increment("ocr.skipped_accessibility_paint")
        return 0
    from core.app_settings import get_capture_settings
    from core.model_residency import can_run_ocr

    if not get_capture_settings().get("ocr_enabled", True) or not can_run_ocr():
        return 0
    from core.ocr import ocr_line_boxes

    lines = ocr_line_boxes(img)
    if not lines:
        return 0
    boxes = [_axis_box(line.get("box")) for line in lines]
    indexes = line_indexes_to_redact([line["text"] for line in lines])
    row_nodes = [
        {"text": lines[index]["text"], "bounds": boxes[index]}
        for index in range(len(lines))
        if boxes[index] is not None
    ]
    row_boxes = [item["bounds"] for item in paired_row_secrets(row_nodes)]
    if not indexes and not row_boxes:
        return 0
    draw = ImageDraw.Draw(img)
    painted = 0
    max_h = img.height * 0.2
    max_area = img.width * img.height * 0.15

    def _paint(box: tuple[int, int, int, int], *, allow_large: bool) -> None:
        nonlocal painted
        left, top, right, bottom = box
        if right - left < 4 or bottom - top < 4:
            return
        if not allow_large and (
            (bottom - top) > max_h or (right - left) * (bottom - top) > max_area
        ):
            return
        pad = 2
        draw.rectangle(
            [
                max(0, left - pad),
                max(0, top - pad),
                min(img.width, right + pad),
                min(img.height, bottom + pad),
            ],
            fill=(0, 0, 0),
        )
        painted += 1

    for index in indexes:
        box = boxes[index]
        if box is not None:
            _paint(box, allow_large=False)
    for box in row_boxes:
        _paint(box, allow_large=False)
    return painted
