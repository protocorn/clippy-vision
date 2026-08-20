#!/usr/bin/env python3
"""Regenerate the README contributors stats table (avatars, profiles, LOC).

Fills the block between:
  <!-- CONTRIBUTORS-STATS:START -->
  <!-- CONTRIBUTORS-STATS:END -->

Sources of truth (union — never rely on only one):
  1. GitHub Contributors API (can lag after merges)
  2. Local git history / commit author mapping
  3. .all-contributorsrc (manual badges + discovered people)

Anyone with real commits who resolves to a GitHub login is always included,
even when the Contributors API has not caught up yet.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
README_PATH = REPO_ROOT / "README.md"
ALL_CONTRIBUTORS_PATH = REPO_ROOT / ".all-contributorsrc"
OWNER = "protocorn"
REPO = "clippy-vision"
AVATAR_SIZE = 64

START = "<!-- CONTRIBUTORS-STATS:START -->"
END = "<!-- CONTRIBUTORS-STATS:END -->"

# GitHub login shape: 1–39 chars, alnum/hyphen, no leading/trailing hyphen.
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
SKIP_LOGINS = {
    "github-actions[bot]",
    "dependabot[bot]",
    "renovate[bot]",
    "imgbot[bot]",
}

# Short, human notes about what each person actually built. Shown in the
# "What they built" column. Anyone not listed gets a link to their commits,
# so new contributors always have a meaningful cell — but add a real line
# here when someone lands something notable.
HIGHLIGHTS = {
    "protocorn": "Designed the core app: agent, vision pipeline, memory system, and the Electron desktop shell.",
    "rusetiq": "Brought Clippy Vision to macOS: native screen capture, permissions, and Apple Silicon + Intel packaging.",
    "cyforkk": "Made errors readable: replaced bare HTTP status codes with real API error messages in chat.",
}

# all-contributors emoji keys we surface in the table
TYPE_EMOJI = {
    "code": "💻",
    "platform": "📦",
    "doc": "📖",
    "design": "🎨",
    "ideas": "🤔",
    "bug": "🐛",
    "maintenance": "🚧",
    "review": "👀",
    "test": "⚠️",
    "infra": "🚇",
    "translation": "🌍",
    "example": "💡",
    "question": "💬",
    "tutorial": "✅",
    "blog": "📝",
    "audio": "🔊",
    "video": "📹",
    "tool": "🔧",
    "fundingFinding": "🔍",
    "financial": "💵",
    "projectManagement": "📆",
    "security": "🛡️",
    "data": "🔣",
    "userTesting": "📓",
    "eventOrganizing": "📋",
}


def github_request(url: str) -> object:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "clippy-vision-contributors",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_contributors() -> list[dict]:
    url = f"https://api.github.com/repos/{OWNER}/{REPO}/contributors?per_page=100&anon=false"
    data = github_request(url)
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected contributors response: {data!r}")
    return [c for c in data if c.get("type") == "User" and c.get("login")]


def fetch_commit_login_map() -> dict[str, str]:
    """Map commit author email / name -> GitHub login via recent commits."""
    mapping: dict[str, str] = {}
    page = 1
    while page <= 10:
        url = (
            f"https://api.github.com/repos/{OWNER}/{REPO}/commits"
            f"?per_page=100&page={page}"
        )
        try:
            batch = github_request(url)
        except urllib.error.HTTPError:
            break
        if not isinstance(batch, list) or not batch:
            break
        for commit in batch:
            author = commit.get("author") or {}
            login = author.get("login")
            if not login:
                continue
            info = (commit.get("commit") or {}).get("author") or {}
            email = (info.get("email") or "").strip().lower()
            name = (info.get("name") or "").strip().lower()
            if email:
                mapping[email] = login
            if name:
                mapping[f"name:{name}"] = login
        if len(batch) < 100:
            break
        page += 1
    return mapping


def parse_noreply_login(email: str) -> str | None:
    # 123456+login@users.noreply.github.com or login@users.noreply.github.com
    m = re.match(
        r"^(?:\d+\+)?([A-Za-z0-9-]+)@users\.noreply\.github\.com$",
        email,
        re.I,
    )
    return m.group(1) if m else None


def compute_loc_by_login(login_map: dict[str, str]) -> dict[str, dict[str, int]]:
    """Return login -> {commits, added, deleted} from git history."""
    result = subprocess.run(
        ["git", "log", "--format=%aN|%aE", "--numstat", "--no-merges"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
        errors="replace",
    )

    stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {"commits": 0, "added": 0, "deleted": 0}
    )
    current_login: str | None = None

    for raw in result.stdout.splitlines():
        line = raw.rstrip("\n")
        if "|" in line and "\t" not in line:
            name, _, email = line.partition("|")
            name_l = name.strip().lower()
            email_l = email.strip().lower()
            login = (
                login_map.get(email_l)
                or login_map.get(f"name:{name_l}")
                or parse_noreply_login(email_l)
                or name.strip()
            )
            current_login = login
            stats[current_login]["commits"] += 1
            continue

        if current_login and "\t" in line:
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            try:
                added = 0 if parts[0] == "-" else int(parts[0])
                deleted = 0 if parts[1] == "-" else int(parts[1])
            except ValueError:
                continue
            stats[current_login]["added"] += added
            stats[current_login]["deleted"] += deleted

    return stats


def load_contribution_types() -> dict[str, list[str]]:
    if not ALL_CONTRIBUTORS_PATH.exists():
        return {}
    data = json.loads(ALL_CONTRIBUTORS_PATH.read_text(encoding="utf-8"))
    out: dict[str, list[str]] = {}
    for person in data.get("contributors") or []:
        login = person.get("login")
        if login:
            out[login] = list(person.get("contributions") or [])
    return out


def format_int(n: int) -> str:
    return f"{n:,}"


def looks_like_login(value: str) -> bool:
    return bool(value) and LOGIN_RE.fullmatch(value) is not None and "[" not in value


def types_cell(types: list[str]) -> str:
    if not types:
        types = ["code"]
    return " ".join(TYPE_EMOJI.get(t, "✨") for t in types)


def highlight_cell(login: str) -> str:
    note = HIGHLIGHTS.get(login)
    if note:
        return note
    commits_url = f"https://github.com/{OWNER}/{REPO}/commits?author={login}"
    return f'<a href="{commits_url}">See their commits →</a>'


def loc_for_login(
    login: str, loc: dict[str, dict[str, int]], fallback_commits: int = 0
) -> dict[str, int]:
    s = loc.get(login) or loc.get(login.lower())
    if s:
        return s
    for key, val in loc.items():
        if key.lower() == login.lower():
            return val
    return {"commits": fallback_commits, "added": 0, "deleted": 0}


def discover_logins_from_git(
    loc: dict[str, dict[str, int]],
    known: set[str],
) -> list[str]:
    """Return GitHub-login-shaped authors present in git but missing from API."""
    found: list[str] = []
    for key, s in loc.items():
        if key.lower() in known:
            continue
        if s["commits"] <= 0:
            continue
        if key.lower() in {b.lower() for b in SKIP_LOGINS}:
            continue
        if not looks_like_login(key):
            # Raw display names ("Sahil Chordia") are skipped — only real logins.
            continue
        found.append(key)
    return found


def ensure_all_contributors_entries(
    logins: list[str], types: dict[str, list[str]]
) -> bool:
    """Persist newly discovered logins into .all-contributorsrc so they stick.

    Returns True if the file was updated.
    """
    if not logins:
        return False
    if ALL_CONTRIBUTORS_PATH.exists():
        data = json.loads(ALL_CONTRIBUTORS_PATH.read_text(encoding="utf-8"))
    else:
        data = {
            "projectName": REPO,
            "projectOwner": OWNER,
            "repoType": "github",
            "repoHost": "https://github.com",
            "files": [],
            "imageSize": 80,
            "commit": False,
            "contributors": [],
        }

    existing = {
        (person.get("login") or "").lower()
        for person in (data.get("contributors") or [])
    }
    changed = False
    for login in logins:
        if login.lower() in existing:
            continue
        data.setdefault("contributors", []).append(
            {
                "login": login,
                "name": login,
                "avatar_url": f"https://github.com/{login}.png",
                "profile": f"https://github.com/{login}",
                "contributions": types.get(login) or ["code"],
            }
        )
        types.setdefault(login, ["code"])
        changed = True
        print(f"Discovered new contributor @{login} — added to .all-contributorsrc")

    if changed:
        ALL_CONTRIBUTORS_PATH.write_text(
            json.dumps(data, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    return changed


def build_table(
    contributors: list[dict],
    loc: dict[str, dict[str, int]],
    types: dict[str, list[str]],
) -> str:
    lines = [
        "",
        "| | Contributor | What they built | Commits | Lines |",
        "| :---: | :--- | :--- | ---: | :---: |",
    ]

    profiles: dict[str, dict] = {}
    for c in contributors:
        login = c["login"]
        if login.lower() in {b.lower() for b in SKIP_LOGINS}:
            continue
        profiles[login] = c

    # Git history is authoritative when the Contributors API lags after a merge.
    for login in discover_logins_from_git(loc, {k.lower() for k in profiles}):
        profiles.setdefault(
            login,
            {
                "login": login,
                "html_url": f"https://github.com/{login}",
                "avatar_url": f"https://github.com/{login}.png",
            },
        )

    # Keep manually listed people even before their first counted commit lands.
    for login in types:
        if login.lower() in {b.lower() for b in SKIP_LOGINS}:
            continue
        profiles.setdefault(
            login,
            {
                "login": login,
                "html_url": f"https://github.com/{login}",
                "avatar_url": f"https://github.com/{login}.png",
            },
        )

    rows: list[tuple[str, dict, dict[str, int]]] = []
    for login, c in profiles.items():
        s = loc_for_login(login, loc, fallback_commits=int(c.get("contributions") or 0))
        rows.append((login, c, s))

    rows.sort(key=lambda r: (-r[2]["added"], -r[2]["commits"], r[0].lower()))

    for login, c, s in rows:
        avatar = c.get("avatar_url") or f"https://github.com/{login}.png"
        profile = c.get("html_url") or f"https://github.com/{login}"
        avatar_md = (
            f'<a href="{profile}">'
            f'<img src="{avatar}" width="{AVATAR_SIZE}" height="{AVATAR_SIZE}" '
            f'alt="{login}"/></a>'
        )
        name_md = (
            f'<a href="{profile}"><b>@{login}</b></a><br/>'
            f"<sub>{types_cell(types.get(login, ['code']))}</sub>"
        )
        lines.append(
            "| "
            f"{avatar_md} | {name_md} | {highlight_cell(login)} | "
            f"{format_int(s['commits'])} | "
            f"+{format_int(s['added'])}&nbsp;/&nbsp;−{format_int(s['deleted'])} |"
        )

    lines.append("")
    lines.append(
        "<sub>Numbers come straight from git history and refresh automatically "
        "on every push to <code>main</code>.</sub>"
    )
    lines.append("")
    return "\n".join(lines)


def replace_section(readme: str, table: str) -> str:
    pattern = re.compile(
        re.escape(START) + r".*?" + re.escape(END),
        re.DOTALL,
    )
    replacement = f"{START}\n{table}{END}"
    if not pattern.search(readme):
        raise SystemExit(f"Could not find {START} ... {END} markers in README.md")
    return pattern.sub(replacement, readme)


def main() -> int:
    try:
        contributors = fetch_contributors()
    except Exception as exc:  # noqa: BLE001 — surface clear CI error
        print(f"Failed to fetch GitHub contributors: {exc}", file=sys.stderr)
        return 1

    login_map = fetch_commit_login_map()
    loc = compute_loc_by_login(login_map)
    types = load_contribution_types()

    api_logins = {c["login"].lower() for c in contributors}
    discovered = discover_logins_from_git(loc, api_logins)
    ensure_all_contributors_entries(discovered, types)

    table = build_table(contributors, loc, types)

    readme = README_PATH.read_text(encoding="utf-8")
    updated = replace_section(readme, table)
    readme_changed = updated != readme
    if readme_changed:
        README_PATH.write_text(updated, encoding="utf-8", newline="\n")

    row_count = table.count("\n| <a href=")
    if not readme_changed and not discovered:
        print("README contributors section already up to date.")
        return 0

    print(
        f"Updated contributors wall with {row_count} people "
        f"({len(discovered)} newly discovered from git)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
