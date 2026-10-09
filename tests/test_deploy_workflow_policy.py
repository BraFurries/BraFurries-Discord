"""Regression checks for Coddy's explicit manual deployment boundary."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_production_workflow_requires_manual_dispatch_on_main():
    workflow = (ROOT / ".github/workflows/prod-deploy.yaml").read_text(encoding="utf-8")
    triggers = workflow.split("\non:\n", 1)[1].split("\njobs:\n", 1)[0]

    assert triggers.strip() == "workflow_dispatch:"
    assert workflow.count("github.ref == 'refs/heads/main'") == 2
    assert "group: coddy-production" in workflow
    assert "labels: [self-hosted, BRFD]" in workflow
    assert "name: Produção" in workflow


def test_legacy_migration_runner_is_not_packaged():
    assert not (ROOT / "scripts/run_migrations.py").exists()
    assert not (ROOT / "migrations").exists()
