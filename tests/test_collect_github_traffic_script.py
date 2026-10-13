"""Guards for the GitHub traffic archive workflow and collector script."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "collect-github-traffic.yml"
SCRIPT = ROOT / "scripts" / "collect_github_traffic.ps1"


def test_collector_fails_on_gh_exit_code():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "$LASTEXITCODE" in text
    assert "401|Bad credentials" in text
    assert "rotate repo secret TRAFFIC_TOKEN" in text


def test_workflow_never_pushes_to_main_from_dispatch_or_a_branch():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "github.event_name == 'schedule'" in text
    assert "github.ref == 'refs/heads/main'" in text
    assert "git push origin HEAD:main" in text
    assert "workflow_dispatch" in text
    assert "actions/upload-artifact@v4" in text


def test_workflow_probes_traffic_api_before_collecting():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "repos/${GITHUB_REPOSITORY}/traffic/views" in text
    assert "unset GITHUB_TOKEN" in text
