"""Regression tests for case-insensitive GitHub login merging."""

import importlib.util
import json
import re
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update_contributors.py"
_SPEC = importlib.util.spec_from_file_location("update_contributors", _SCRIPT)
uc = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(uc)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _logins_in_table(table: str) -> list[str]:
    return re.findall(r"<b>@([^<]+)</b>", table)


def test_noreply_login_keeps_github_canonical_case():
    assert (
        uc.parse_noreply_login("177975472+Furox-Art@users.noreply.github.com")
        == "Furox-Art"
    )
    assert uc.parse_noreply_login("50778230+KeuqS@users.noreply.github.com") == "KeuqS"


def test_resolve_commit_login_does_not_lowercase_noreply_mailbox():
    assert (
        uc.resolve_commit_login(
            "Furox",
            "177975472+Furox-Art@users.noreply.github.com",
            {},
        )
        == "Furox-Art"
    )
    mapped = uc.resolve_commit_login(
        "Aykut Güney",
        "50778230+KeuqS@users.noreply.github.com",
        {"50778230+keuqs@users.noreply.github.com": "KeuqS"},
    )
    assert mapped == "KeuqS"


def test_prefer_login_spelling_keeps_github_case():
    assert uc.prefer_login_spelling("furox-art", "Furox-Art") == "Furox-Art"
    assert uc.prefer_login_spelling("Furox-Art", "furox-art") == "Furox-Art"
    assert uc.prefer_login_spelling("KeuqS", "keuqs") == "KeuqS"


def test_fold_stats_merges_case_variants_onto_canonical_login():
    loc = {
        "furox-art": {"commits": 1, "added": 10, "deleted": 1},
        "Furox-Art": {"commits": 2, "added": 5, "deleted": 0},
    }
    folded = uc.fold_stats_by_login(loc, {"furox-art": "Furox-Art"})
    assert list(folded) == ["Furox-Art"]
    assert folded["Furox-Art"] == {"commits": 3, "added": 15, "deleted": 1}


def test_merge_contributor_records_keeps_custom_name_and_union_types():
    people = [
        {
            "login": "furox-art",
            "name": "Furox",
            "avatar_url": "https://github.com/furox-art.png",
            "profile": "https://github.com/furox-art",
            "contributions": ["code"],
        },
        {
            "login": "Furox-Art",
            "name": "Furox-Art",
            "avatar_url": "https://github.com/Furox-Art.png",
            "profile": "https://github.com/Furox-Art",
            "contributions": ["doc"],
        },
    ]
    merged = uc.merge_contributor_records(people, {"furox-art": "Furox-Art"})
    assert len(merged) == 1
    assert merged[0]["login"] == "Furox-Art"
    assert merged[0]["name"] == "Furox"
    assert merged[0]["contributions"] == ["code", "doc"]
    assert merged[0]["profile"] == "https://github.com/Furox-Art"
    assert merged[0]["avatar_url"] == "https://github.com/Furox-Art.png"


def test_merge_contributor_records_rewrites_login_shaped_name():
    people = [
        {
            "login": "keuqs",
            "name": "keuqs",
            "avatar_url": "https://github.com/keuqs.png",
            "profile": "https://github.com/keuqs",
            "contributions": ["code"],
        }
    ]
    merged = uc.merge_contributor_records(people, {"keuqs": "KeuqS"})
    assert merged[0]["login"] == "KeuqS"
    assert merged[0]["name"] == "KeuqS"
    assert merged[0]["profile"] == "https://github.com/KeuqS"


def test_build_table_collapses_api_and_config_case_variants():
    contributors = [
        {
            "login": "Furox-Art",
            "html_url": "https://github.com/Furox-Art",
            "avatar_url": "https://github.com/Furox-Art.png",
            "contributions": 1,
        },
        {
            "login": "KeuqS",
            "html_url": "https://github.com/KeuqS",
            "avatar_url": "https://github.com/KeuqS.png",
            "contributions": 1,
        },
    ]
    loc = {
        "Furox-Art": {"commits": 1, "added": 50, "deleted": 0},
        "furox-art": {"commits": 1, "added": 50, "deleted": 0},
        "KeuqS": {"commits": 1, "added": 20, "deleted": 0},
        "keuqs": {"commits": 1, "added": 20, "deleted": 0},
    }
    types = {"furox-art": ["code", "doc"], "keuqs": ["code"]}
    table = uc.build_table(contributors, loc, types)
    logins = _logins_in_table(table)
    assert logins.count("Furox-Art") == 1
    assert logins.count("KeuqS") == 1
    assert "furox-art" not in logins
    assert "keuqs" not in logins
    assert "📖" in table
    lowered = [login.lower() for login in logins]
    assert len(lowered) == len(set(lowered))


def test_build_table_collapses_when_contributors_api_lags():
    loc = {
        "Furox-Art": {"commits": 1, "added": 10, "deleted": 0},
        "KeuqS": {"commits": 1, "added": 8, "deleted": 0},
    }
    types = {"furox-art": ["code"], "keuqs": ["code"]}
    canonical = {"furox-art": "Furox-Art", "keuqs": "KeuqS"}
    table = uc.build_table([], loc, types, canonical)
    logins = _logins_in_table(table)
    assert logins == ["Furox-Art", "KeuqS"]


def test_build_table_does_not_invent_other_duplicates():
    loc = {
        "Afrin-banu-A": {"commits": 2, "added": 12, "deleted": 0},
        "ABarpanda": {"commits": 4, "added": 30, "deleted": 0},
    }
    types = {"Afrin-banu-A": ["code"], "ABarpanda": ["code"]}
    table = uc.build_table([], loc, types)
    logins = _logins_in_table(table)
    assert logins == ["ABarpanda", "Afrin-banu-A"]


def test_rewrite_all_contributors_dedupes_file(tmp_path, monkeypatch):
    rc = tmp_path / ".all-contributorsrc"
    rc.write_text(
        json.dumps(
            {
                "contributors": [
                    {
                        "login": "furox-art",
                        "name": "Furox",
                        "avatar_url": "https://github.com/furox-art.png",
                        "profile": "https://github.com/furox-art",
                        "contributions": ["code"],
                    },
                    {
                        "login": "Furox-Art",
                        "name": "Furox-Art",
                        "avatar_url": "https://github.com/Furox-Art.png",
                        "profile": "https://github.com/Furox-Art",
                        "contributions": ["doc"],
                    },
                    {
                        "login": "keuqs",
                        "name": "keuqs",
                        "avatar_url": "https://github.com/keuqs.png",
                        "profile": "https://github.com/keuqs",
                        "contributions": ["code"],
                    },
                ]
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(uc, "ALL_CONTRIBUTORS_PATH", rc)
    changed = uc.rewrite_all_contributors(
        {"furox-art": "Furox-Art", "keuqs": "KeuqS"}
    )
    assert changed is True
    people = json.loads(rc.read_text(encoding="utf-8"))["contributors"]
    logins = [person["login"] for person in people]
    assert logins == ["Furox-Art", "KeuqS"]
    assert people[0]["name"] == "Furox"
    assert people[0]["contributions"] == ["code", "doc"]
    assert people[1]["name"] == "KeuqS"


def test_readme_contributor_logins_are_unique_case_insensitive():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    start = readme.index("<!-- CONTRIBUTORS-STATS:START -->")
    end = readme.index("<!-- CONTRIBUTORS-STATS:END -->")
    logins = _logins_in_table(readme[start:end])
    lowered = [login.lower() for login in logins]
    duplicates = sorted({login for login in lowered if lowered.count(login) > 1})
    assert duplicates == [], f"Duplicate contributor logins in README: {duplicates}"
    assert "furox-art" not in lowered or logins[lowered.index("furox-art")] == "Furox-Art"
    assert "keuqs" not in lowered or logins[lowered.index("keuqs")] == "KeuqS"


def test_all_contributorsrc_logins_are_unique_case_insensitive():
    data = json.loads((REPO_ROOT / ".all-contributorsrc").read_text(encoding="utf-8"))
    logins = [person["login"] for person in data["contributors"] if person.get("login")]
    lowered = [login.lower() for login in logins]
    duplicates = sorted({login for login in lowered if lowered.count(login) > 1})
    assert duplicates == [], f"Duplicate logins in .all-contributorsrc: {duplicates}"
    by_lower = {login.lower(): login for login in logins}
    if "furox-art" in by_lower:
        assert by_lower["furox-art"] == "Furox-Art"
    if "keuqs" in by_lower:
        assert by_lower["keuqs"] == "KeuqS"
