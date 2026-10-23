"""The release workflow must build unsigned macOS installers on PRs, like Windows/Linux."""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "release.yml"
PKG = REPO / "electron-ui" / "package.json"


def _macos_job_block(text: str) -> str:
    start = text.index("  build-macos:")
    linux = text.index("  build-linux:")
    return text[start:linux]


def test_macos_job_runs_on_prs_and_tag_pushes():
    text = WORKFLOW.read_text(encoding="utf-8")
    job = _macos_job_block(text)
    assert "runs-on: macos-latest" in job
    if_line = next(
        line.strip()
        for line in job.splitlines()
        if line.strip().startswith("if:")
    )
    assert "github.event_name == 'pull_request'" in if_line
    assert "github.event_name == 'push'" in if_line
    # workflow_dispatch may still opt in; PRs/tags must not depend on that flag alone.
    assert if_line != "if: github.event_name == 'workflow_dispatch' && inputs.include_macos"


def test_macos_build_is_unsigned_publish_never_and_uploads_dmg_or_zip():
    text = WORKFLOW.read_text(encoding="utf-8")
    job = _macos_job_block(text)
    assert "CSC_IDENTITY_AUTO_DISCOVERY: false" in job
    assert "npm run dist:mac" in job
    assert "name: macos-installer" in job
    assert "electron-ui/dist/*.dmg" in job
    assert "electron-ui/dist/*.zip" in job
    pkg = PKG.read_text(encoding="utf-8")
    assert '"dist:mac": "electron-builder --mac --publish never"' in pkg


def test_macos_job_does_not_add_apple_or_signing_secrets():
    text = WORKFLOW.read_text(encoding="utf-8")
    job = _macos_job_block(text)
    forbidden = (
        "APPLE_ID",
        "APPLE_APP_SPECIFIC_PASSWORD",
        "APP_STORE_CONNECT",
        "CSC_LINK",
        "CSC_KEY_PASSWORD",
        "CSC_NAME",
        "secrets.APPLE",
    )
    for token in forbidden:
        assert token not in job, f"macOS job must not introduce {token}"
    # Existing Windows/Linux release upload uses GITHUB_TOKEN; macOS may too.
    # The job itself must not require a new secret to *build*.
    assert "CSC_IDENTITY_AUTO_DISCOVERY: false" in job
