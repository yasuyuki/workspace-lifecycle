"""Model-check the finish and integration steps of service.py with TLC.

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
SPEC = 'FinishIntegration.tla'
TLC_VERSION = 'TLC2 Version 2.19 of 08 August 2024'

# None: every invariant holds. A rule name: TLC reports that rule as violated.
EXPECTED = {
    'gated': None,
    'ungated': None,
}

# (name, code it stands for, configuration, rule, model text, broken text)
MUTANTS = [
    ('finish keeps an integration prepared for an older HEAD',
     'service.py finish() `_withdraw_prepared()` before its lease (#80)', 'gated',
     'FixCanFinish',
     'CASE Deferred /\\ intent.src # head ->', 'CASE FALSE ->'),
    ('checks.verify accepts checks that have not succeeded',
     'checks.py verify() `if unmet: raise`', 'gated',
     'OnlyCheckedWorkReachesDefault',
     'THEN /\\ gate\' = g', 'THEN /\\ gate\' = TRUE'),
    ('a prepared integration is pushed before its checks succeed',
     'service.py _integrate() `if not gate: return ... prepared`', 'gated',
     'OnlyCheckedWorkReachesDefault',
     'THEN IF ~gate THEN pc\' = "await"', 'THEN IF FALSE THEN pc\' = "await"'),
    ('withdrawal drops the intent but keeps the merge',
     'service.py _withdraw() `git checkout -B <target> <before>`', 'gated',
     'NoForgottenPreparedMerge',
     'ELSE tLocal\' = intent.before /\\ pc\' = "withdrawPop"',
     'ELSE UNCHANGED tLocal /\\ pc\' = "withdrawPop"'),
    ('another task integrates beside a pending merge',
     'service.py _integrate() `_refuse_pending_merge(state, target_branch)`', 'gated',
     'OnlyCheckedWorkReachesDefault',
     '/\\ ~otherDone /\\ pc \\notin EdgeLeased /\\ intent.kind # "merge"',
     '/\\ ~otherDone /\\ pc \\notin EdgeLeased'),
    ('an ungated finish skips the task checkout validation',
     'service.py finish() `_validate(repo, task[\'validation\'])`', 'ungated',
     'OnlyCheckedWorkReachesDefault',
     'IF Candidate \\/ valid[head] THEN "push"', 'IF TRUE THEN "push"'),
    ('integration skips the validation in the default checkout',
     'service.py _integrate() `_validate(target_worktree, ...)`', 'gated',
     'OnlyCheckedWorkReachesDefault',
     'IF validT[intent.src] THEN "gate"', 'IF TRUE THEN "gate"'),
    ('the integration is recorded before its push',
     'service.py _integrate() order of _push and `item[\'integrated\'] = record`', 'ungated',
     'IntegratedIsPushed',
     'pc = "accept"\n  /\\ rec\' = [rec EXCEPT !.kind = "acceptance"]\n  /\\ pc\' = "pushTarget"\n  /\\ UNCHANGED <<task, fixRefused, ci, topic, integrated,',
     'pc = "accept"\n  /\\ rec\' = [rec EXCEPT !.kind = "acceptance"] /\\ integrated\' = intent.src\n  /\\ pc\' = "pushTarget"\n  /\\ UNCHANGED <<task, fixRefused, ci, topic,'),
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
