"""Model-check which remote checks finish waits for and when it integrates, with TLC.

Exit 0 when every configuration ends as EXPECTED (and, with --mutants, every
mutant breaks its rule); 1 otherwise; 2 when TLC cannot run.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

MODEL = Path(__file__).resolve().parents[1] / 'model'
SPEC = 'RequiredChecks.tla'
TLC_VERSION = 'TLC2 Version 2.19 of 08 August 2024'

# None: every invariant and property holds. A rule name: TLC reports that rule
# as violated, a finding about the current code filed as the Issue in the comment.
EXPECTED = {
    'steady': None,
    'default-adds-check': 'FinishCompletes',            # workspace-lifecycle#73
    'default-adds-check-effects': None,
    'lost-event': 'FinishCompletes',                    # workspace-lifecycle#73
}

# (name, code it stands for, configuration, rule, model text, broken text)
MUTANTS = [
    ('integration ignores checks the default branch declares and the task omits',
     'checks.require() in service.py _check_policy()', 'default-adds-check',
     'GateHolds',
     '  /\\ IF ~(defDecl \\subseteq required)\n       THEN Refuse("omit")',
     '  /\\ IF FALSE\n       THEN Refuse("omit")'),
    ("integration ignores the checks the commit's own declaration adds",
     "service.py _integrate() `gated_checks = required_checks + (proposed['added'] ...)`", 'steady',
     'GateHolds',
     'Gate == required \\cup Added',
     'Gate == required'),
    ('finish runs the plan before the declaration check',
     'service.py finish() checks.require() among the early refusals', 'default-adds-check-effects',
     'NoEffectBeforeKnownRefusal',
     '     ELSE IF ~(defDecl \\subseteq required) THEN Refuse("omit")\n',
     ''),
    ('the wait counts a missing check as done',
     'checks.watch() `unmet` and PENDING', 'lost-event',
     'GateHolds',
     '  /\\ IF Gate \\subseteq Succeeded\n',
     '  /\\ IF Gate \\subseteq Succeeded \\cup {c \\in Checks : runs[c] = "none"}\n'),
]


def tlc(java, jar, directory, config):
    with tempfile.TemporaryDirectory() as temp:
        proc = subprocess.run(
            [java, '-XX:+UseParallelGC', '-Djava.io.tmpdir=' + temp, '-cp', jar, 'tlc2.TLC',
             '-workers', '1', '-metadir', str(Path(temp) / 'meta'),
             '-config', config + '.cfg', SPEC],
            cwd=directory, capture_output=True, text=True, encoding='utf-8', errors='replace')
    out = proc.stdout + proc.stderr
    if TLC_VERSION not in out:
        fail({'error': 'expected ' + TLC_VERSION, 'output': out[-2000:]})
    violated = re.search(r'Invariant (\w+) is violated', out)
    if not violated and 'Temporal properties were violated' in out:
        # Each configuration names at most one temporal property.
        violated = re.search(r'^PROPERTIES\s+(\w+)', (Path(directory) / (config + '.cfg')).read_text(), re.M)
    if violated:
        steps = re.findall(r'^State \d+: <(\w+)', out, re.M)
        return {'violated': violated.group(1), 'path': steps}
    if 'Model checking completed. No error has been found.' in out:
        states = re.search(r'(\d+) distinct states found', out)
        return {'violated': None, 'distinct_states': int(states.group(1))}
    fail({'error': 'TLC did not finish', 'config': config, 'output': out[-3000:]})


def fail(report):
    print(json.dumps(report))
    sys.exit(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--java', default=os.environ.get('JAVA') or shutil.which('java'))
    parser.add_argument('--tla2tools', default=os.environ.get('TLA2TOOLS'))
    parser.add_argument('--mutants', action='store_true', help='also check that each mutant breaks its rule')
    args = parser.parse_args()
    if not args.java or not args.tla2tools or not Path(args.tla2tools).is_file():
        print(json.dumps({'error': 'give --java and --tla2tools, or set JAVA and TLA2TOOLS'}))
        return 2
    ok = True
    for config, expected in EXPECTED.items():
        result = tlc(args.java, args.tla2tools, MODEL, config)
        match = result['violated'] == expected
        ok &= match
        print(json.dumps({'config': config, 'expected': expected, 'match': match, **result}))
    if args.mutants:
        for name, code, config, rule, old, new in MUTANTS:
            with tempfile.TemporaryDirectory() as temp:
                copy = Path(temp)
                for path in MODEL.iterdir():
                    shutil.copy(path, copy)
                spec = copy / SPEC
                text = spec.read_text(encoding='utf-8')
                if text.count(old) != 1:
                    print(json.dumps({'mutant': name, 'error': 'model text not found once; update MUTANTS'}))
                    ok = False
                    continue
                spec.write_bytes(text.replace(old, new).encode('utf-8'))
                result = tlc(args.java, args.tla2tools, copy, config)
            caught = result['violated'] == rule
            ok &= caught
            print(json.dumps({'mutant': name, 'code': code, 'config': config, 'caught': caught, **result}))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
