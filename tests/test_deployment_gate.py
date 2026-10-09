from scripts.ci.deployment_gate import MAIN_REF, SCHEMA_SENSITIVE_PATHS, ZERO_SHA, evaluate_gate


BEFORE = '1' * 40
HEAD = '2' * 40


def decide(*, paths=(), event='push', ref=MAIN_REF, before=BEFORE, workflow_sha=HEAD, current_main=HEAD, base_available=True):
    return evaluate_gate(
        event_name=event,
        ref=ref,
        before=before,
        workflow_sha=workflow_sha,
        current_main_sha=current_main,
        changed_paths=paths,
        base_available=base_available,
    )


def test_normal_runtime_python_push_allows_automatic_deploy():
    assert decide(paths=['core/bot_status.py']).deploy_allowed is True


def test_new_migration_blocks_automatic_deploy():
    decision = decide(paths=['migrations/20260907_01_example.sql'])
    assert decision.deploy_allowed is False
    assert decision.sensitive_paths == ('migrations/20260907_01_example.sql',)


def test_changed_or_removed_existing_migration_blocks_automatic_deploy():
    assert decide(paths=['migrations/20260101_existing.sql']).deploy_allowed is False


def test_migration_runner_blocks_automatic_deploy():
    assert decide(paths=['scripts/run_migrations.py']).deploy_allowed is False


def test_production_workflow_and_gate_helper_block_their_own_auto_activation():
    decision = decide(paths=['.github/workflows/prod-deploy.yaml', 'scripts/ci/deployment_gate.py'])
    assert decision.deploy_allowed is False
    assert len(decision.sensitive_paths) == 2


def test_runtime_schema_authority_paths_block_automatic_deploy():
    assert SCHEMA_SENSITIVE_PATHS == {'core/database.py', 'cogs/xp.py'}
    for path in SCHEMA_SENSITIVE_PATHS:
        decision = decide(paths=[path])
        assert decision.deploy_allowed is False
        assert decision.sensitive_paths == (path,)


def test_current_manual_dispatch_on_main_bypasses_path_safety_without_push_base():
    decision = decide(
        event='workflow_dispatch',
        before='',
        paths=['migrations/example.sql', 'core/database.py', 'cogs/xp.py'],
    )
    assert decision.deploy_allowed is True


def test_stale_manual_dispatch_is_skipped():
    decision = decide(event='workflow_dispatch', current_main='3' * 40)
    assert decision.deploy_allowed is False
    assert decision.reason == 'superseded by newer main commit'


def test_manual_dispatch_outside_main_is_skipped():
    assert decide(event='workflow_dispatch', ref='refs/heads/feature').deploy_allowed is False


def test_superseded_push_is_skipped():
    decision = decide(current_main='3' * 40)
    assert decision.deploy_allowed is False
    assert decision.reason == 'superseded by newer main commit'


def test_invalid_missing_or_all_zero_push_base_fails_closed():
    for before in ('', 'invalid', ZERO_SHA):
        assert decide(before=before).deploy_allowed is False


def test_unavailable_push_base_fails_closed():
    assert decide(base_available=False).deploy_allowed is False


def test_docs_only_push_does_not_trigger_migration_safety_gate():
    assert decide(paths=['README.md', 'docs/documentacao.html']).deploy_allowed is True
