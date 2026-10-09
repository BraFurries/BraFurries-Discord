from __future__ import annotations

import argparse
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


MAIN_REF = 'refs/heads/main'
ZERO_SHA = '0' * 40
SHA_PATTERN = re.compile(r'^[0-9a-fA-F]{40}$')
PROTECTED_PATHS = {
    'scripts/run_migrations.py',
    '.github/workflows/prod-deploy.yaml',
    'scripts/ci/deployment_gate.py',
}
SCHEMA_SENSITIVE_PATHS = {
    'core/database.py',
    'cogs/xp.py',
}


@dataclass(frozen=True)
class GateDecision:
    deploy_allowed: bool
    reason: str
    sensitive_paths: tuple[str, ...] = ()


def is_sensitive_path(path: str) -> bool:
    normalized = path.replace('\\', '/')
    while normalized.startswith('./'):
        normalized = normalized[2:]
    return (
        normalized == 'migrations'
        or normalized.startswith('migrations/')
        or normalized in PROTECTED_PATHS
        or normalized in SCHEMA_SENSITIVE_PATHS
    )


def evaluate_gate(
    *,
    event_name: str,
    ref: str,
    before: str,
    workflow_sha: str,
    current_main_sha: str,
    changed_paths: Iterable[str] = (),
    base_available: bool = True,
) -> GateDecision:
    if ref != MAIN_REF:
        return GateDecision(False, 'workflow ref is not main')
    if workflow_sha.lower() != current_main_sha.lower():
        return GateDecision(False, 'superseded by newer main commit')
    if event_name == 'workflow_dispatch':
        return GateDecision(True, 'manual workflow_dispatch on main')
    if event_name != 'push':
        return GateDecision(False, 'unsupported workflow event')
    if not SHA_PATTERN.fullmatch(before or '') or before.lower() == ZERO_SHA:
        return GateDecision(False, 'invalid or missing push base')
    if not base_available:
        return GateDecision(False, 'push base commit is unavailable')

    sensitive_paths = tuple(sorted(path for path in changed_paths if is_sensitive_path(path)))
    if sensitive_paths:
        return GateDecision(False, 'production-sensitive files changed', sensitive_paths)
    return GateDecision(True, 'automatic push passed production safety checks')


def _git_changed_paths(before: str, workflow_sha: str) -> tuple[bool, list[str]]:
    base_check = subprocess.run(
        ['git', 'cat-file', '-e', f'{before}^{{commit}}'],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if base_check.returncode != 0:
        return False, []
    diff = subprocess.run(
        ['git', 'diff', '--no-renames', '--name-only', '-z', before, workflow_sha],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if diff.returncode != 0:
        return False, []
    return True, [item.decode('utf-8', errors='replace') for item in diff.stdout.split(b'\0') if item]


def _safe_summary_path(path: str) -> str:
    return ''.join(character if character.isprintable() else '?' for character in path).replace('`', "'")


def _write_results(decision: GateDecision, output_path: Path, summary_path: Path) -> None:
    with output_path.open('a', encoding='utf-8') as output:
        output.write(f"deploy_allowed={'true' if decision.deploy_allowed else 'false'}\nreason={decision.reason}\n")
    heading = 'Production deploy allowed.' if decision.deploy_allowed else 'Automatic production deploy skipped.'
    lines = [f'## {heading}', '', f'Reason: {decision.reason}.']
    if decision.sensitive_paths:
        lines.extend(['', 'Production-sensitive paths:'])
        lines.extend(f'- `{_safe_summary_path(path)}`' for path in decision.sensitive_paths)
    if not decision.deploy_allowed:
        lines.extend(['', 'Start a manual workflow_dispatch from the current main head if deployment is intended.'])
    with summary_path.open('a', encoding='utf-8') as summary:
        summary.write('\n'.join(lines) + '\n')


def main() -> None:
    parser = argparse.ArgumentParser(description='Decide whether the Coddy production deploy may run.')
    parser.add_argument('--event-name', required=True)
    parser.add_argument('--ref', required=True)
    parser.add_argument('--before', default='')
    parser.add_argument('--workflow-sha', required=True)
    parser.add_argument('--current-main-sha', required=True)
    parser.add_argument('--github-output', type=Path, required=True)
    parser.add_argument('--step-summary', type=Path, required=True)
    args = parser.parse_args()

    base_available = True
    changed_paths: list[str] = []
    if args.event_name == 'push' and SHA_PATTERN.fullmatch(args.before or '') and args.before.lower() != ZERO_SHA:
        base_available, changed_paths = _git_changed_paths(args.before, args.workflow_sha)

    decision = evaluate_gate(
        event_name=args.event_name,
        ref=args.ref,
        before=args.before,
        workflow_sha=args.workflow_sha,
        current_main_sha=args.current_main_sha,
        changed_paths=changed_paths,
        base_available=base_available,
    )
    _write_results(decision, args.github_output, args.step_summary)


if __name__ == '__main__':
    main()
