"""Regression checks for Coddy's protected production deployment boundary."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_production_workflow_only_runs_from_main_and_supports_manual_deploy():
    workflow = (ROOT / ".github/workflows/prod-deploy.yaml").read_text(encoding="utf-8")
    triggers = workflow.split("\non:\n", 1)[1].split("\njobs:\n", 1)[0]

    assert triggers.strip() == "workflow_dispatch:\n  push:\n    branches:\n      - main"
    assert workflow.count("github.ref == 'refs/heads/main'") >= 2
    assert "github.event_name == 'workflow_dispatch'" in workflow
    assert "vars.CODDY_AUTO_DEPLOY_ENABLED == 'true'" in workflow
    assert "needs: secret-scan" in workflow
    assert "needs: build-image" in workflow
    assert "Refusing stale deployment: main has advanced." in workflow
    assert "git --log-opts=\"--all\" --redact=100" in workflow
    assert "group: coddy-production" in workflow
    assert "labels: [self-hosted, BRFD]" in workflow
    assert "name: Produção" in workflow
    assert "PROD_ENV_FILE: ${{ secrets.PROD_ENV_FILE }}" in workflow
    assert "workflow_run:" not in triggers
    assert "pull_request:" not in triggers


def test_legacy_migration_runner_is_not_packaged():
    assert not (ROOT / "scripts/run_migrations.py").exists()
    assert not (ROOT / "migrations").exists()
