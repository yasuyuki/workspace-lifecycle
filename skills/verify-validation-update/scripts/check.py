"""Model-check finish() in the task checkout and update_validation() with TLC.

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
SPEC = 'ValidationUpdate.tla'
PROPERTY = 'FinishCompletes'
TLC_VERSION = 'TLC2 Version 2.19 of 08 August 2024'

# None: every invariant and property holds. A rule name: TLC reports that rule as violated.
EXPECTED = {
    'with-restore': None,
    'commit-only': None,
    'empty-plan': None,
}

# (name, code it stands for, configuration, rule, model text, broken text)
MUTANTS = [
    ('update-validation releases a finish that started a restore',
     'service.py _effectless_finish() `intent.get(\'actions\') == {}`', 'with-restore',
     'NoUnrecordedEffect',
     'intent.kind = "finish" /\\ intent.phase = None /\\ ~intent.tree',
     'intent.kind = "finish" /\\ ~intent.tree'),
    ('a restore removes its file before saving its phase',
     'service.py _resolve_files() `phase: restoring` saved before the unlink', 'with-restore',
     'NoUnrecordedEffect',
     'ELSE intent\' = [intent EXCEPT !.phase = "restoring"] /\\ pc\' = "restoreEffect"',
     'ELSE pc\' = "restoreEffect" /\\ UNCHANGED intent'),
    ('a failed validation drops the finish intent',
     'service.py finish() keeps the intent when _validate raises', 'commit-only',
     'NoUnrecordedEffect',
     '/\\ pc\' = IF validator = "good" THEN "push" ELSE "idle"\n  /\\ UNCHANGED <<head, staged, restored, validator, preflight, intent, accepted, kills>>',
     '/\\ pc\' = IF validator = "good" THEN "push" ELSE "idle"\n  /\\ intent\' = IF validator = "good" THEN intent ELSE NoIntent\n  /\\ UNCHANGED <<head, staged, restored, validator, preflight, accepted, kills>>'),
    ('finish does not adopt its own unrecorded commit',
     'service.py finish() `intent[\'committed\'] = head(repo)` after a matching commit_tree', 'commit-only',
     'FinishCompletes',
     '[] intent.tree /\\ head = "c1" /\\ intent.initial = "c0" ->',
     '[] FALSE ->'),
    ('update-validation refuses a finish whose commit is recorded',
     'service.py _recorded_finish() `committed` branch', 'commit-only',
     'FinishCompletes',
     'ELSE head = "c1" /\\ intent.initial = "c0"', 'ELSE FALSE'),
    ('update-preflight refuses every pending finish (#69)',
     'service.py update_preflight() calling _correctable_finish()', 'empty-plan',
     'FinishCompletes',
     '/\\ preflight\' = "good"',
     '/\\ intent.kind = None /\\ preflight\' = "good"'),
    ('update-preflight releases a finish whose commit is recorded',
     'service.py _correctable_finish() releasing a finish that _recorded_finish() keeps', 'commit-only',
     'NoUnrecordedEffect',
     '/\\ preflight\' = "good"\n  /\\ intent\' = IF Effectless THEN NoIntent ELSE intent',
     '/\\ preflight\' = "good"\n  /\\ intent\' = NoIntent'),
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
        violated = re.match(r'(\w+)', PROPERTY)
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
