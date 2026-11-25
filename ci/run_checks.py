"""Run one part of the ResumeOps CI checks: python ci/run_checks.py <part>   (python ci/run_checks.py --list)

CI runs the parts as parallel jobs. Together they run every unit test and every dashboard check exactly once:
unit tests are split by module (unit-a takes the slow modules, unit-b everything else, including any module
that fails to import), and the script fails if a dashboard/check_*.py file is not assigned to a part.
Each command's time is printed so the split can be rebalanced from the CI log.
"""
import glob
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Checks never reach a real Ollama: CI runs on the same PC as production, whose GPU serves resumes. A model call a
# test forgot to stub fails fast here (connection refused) instead of generating on the shared GPU. Checks that
# need a model start their own fake server and pass its URL explicitly (dashboard/check_live.py).
import os  # noqa: E402
os.environ['OLLAMA_URL'] = 'http://127.0.0.1:9'
SLOW_UNIT_MODULES = {'test_live_progress', 'test_acceptance'}
CHECKS = {
    'live': [['check_live.py']],
    'browser': [['check_dashboard.py', '.'], ['check_builder.py'], ['check_cloudflare_access.py'],
                ['check_session.py'], ['check_ai_builder.py'], ['check_automation.py'], ['check_intake.py']],
}
PARTS = ('unit-a', 'unit-b', 'live', 'browser')


def assigned():
    names = [cmd[0] for commands in CHECKS.values() for cmd in commands]
    present = sorted(Path(p).name for p in glob.glob(str(ROOT / 'dashboard' / 'check_*.py')))
    missing = sorted(set(present) - set(names))
    duplicated = sorted({n for n in names if names.count(n) > 1})
    if missing or duplicated:
        sys.exit(f'Every dashboard check must run exactly once: unassigned={missing} duplicated={duplicated}')


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def unit_tests(part):
    # Same discovery as `python -m unittest discover -s tests` from the repository root, which also puts the
    # root on sys.path so the tests can import resume_bot and dashboard modules.
    sys.path.insert(0, str(ROOT))
    tests = list(flatten(unittest.defaultTestLoader.discover(str(ROOT / 'tests'))))
    slow = part == 'unit-a'
    picked = [t for t in tests if (type(t).__module__ in SLOW_UNIT_MODULES) == slow]
    print(f'{part}: {len(picked)} of {len(tests)} unit tests', flush=True)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(picked))
    return result.wasSuccessful() and (result.testsRun > 0 or not slow)


def run(command):
    started = time.monotonic()
    print(f'\n=== {" ".join(command)}', flush=True)
    code = subprocess.run(command, cwd=ROOT).returncode
    print(f'=== {"ok" if code == 0 else f"FAILED ({code})"} in {time.monotonic() - started:.1f}s: {" ".join(command)}',
          flush=True)
    return code == 0


def main(part):
    assigned()
    if part == '--list':
        print('\n'.join(PARTS))
        return True
    if part not in PARTS:
        sys.exit(f'Unknown part {part!r}; expected one of {PARTS}')
    started = time.monotonic()
    if part.startswith('unit-'):
        ok = unit_tests(part)
    else:
        results = [run([sys.executable, str(Path('dashboard') / cmd[0]), *cmd[1:]]) for cmd in CHECKS[part]]
        if part == 'browser':
            results.append(run(['node', '--test', *sorted(glob.glob('tests/js/*.test.js', root_dir=ROOT))]))
        ok = all(results)
    print(f'\n{part}: {"passed" if ok else "FAILED"} in {time.monotonic() - started:.1f}s', flush=True)
    return ok


if __name__ == '__main__':
    sys.exit(0 if main(sys.argv[1] if len(sys.argv) > 1 else '--list') else 1)
