"""Model-check service.py _integrate() for a child of a parent task with TLC.

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
SPEC = 'ParentIntegration.tla'
PROPERTY = 'ChildReachesDefault'
TLC_VERSION = 'TLC2 Version 2.19 of 08 August 2024'

# None: every invariant and property holds. A rule name: TLC reports that rule as violated.
EXPECTED = {
    'child-only': None,
    'parent-finish': None,
    'parent-session': None,
    'parent-edge-kill': None,
    'parent-dependency': None,
}

# (name, code it stands for, configuration, rule, model text, broken text)
MUTANTS = [
    ('the child merges onto a parent HEAD the parent did not accept',
     "service.py _integrate() `if parent and target_head != parent['acceptance']['commit']`", 'parent-session',
     'ParentAcceptsOnlyItsOwn',
     'ELSE IF dIntent = {} /\\ pHead = pAcc', 'ELSE IF dIntent = {}'),
    ("the child merges while the parent's own integration is pending (#85)",
     'service.py _integrate() `_refuse_pending_merge(state, target_branch, direction.get(\'task\'))`',
     'parent-edge-kill', 'ChildReachesDefault',
     'ELSE IF dIntent = {} /\\ pHead = pAcc', 'ELSE IF pHead = pAcc'),
    ("the parent integrates while a child's merge into it is pending (#85)",
     'service.py _integrate() `_refuse_pending_merge(state, source_branch)`', 'parent-edge-kill',
     'ChildReachesDefault',
     ' \\/ (dIntent = {} /\\ cIntent.open)', ''),
    ("the parent's run starts while a child's merge into it is pending (#86)",
     'service.py before_run() `_refuse_pending_merge(state, branch_for_task(repo, task))`', 'parent-session',
     'ChildReachesDefault',
     'lockP = None /\\ ~cIntent.open', 'lockP = None'),
    ('the dependency check refuses the merge the child itself made (#87)',
     "service.py _integrate() dependency loop `continue` for the parent under the child's merge intent",
     'parent-dependency', 'ChildReachesDefault',
     'pHead # pAcc /\\ ~cIntent.open', 'pHead # pAcc'),
    ('the parent finish ignores a pending child merge',
     'service.py finish() `_refuse_pending_merge(state, branch)`', 'parent-finish',
     'ChildCheckedInParent',
     'IF cIntent.open \\/ (dIntent', 'IF FALSE \\/ (dIntent'),
    ('the child record leaves the parent acceptance behind',
     "service.py _integrate() `parent['acceptance'] = {..., 'commit': merged, ...}`", 'child-only',
     'ChildRecordInParent',
     "/\\ pAcc' = cIntent.merged /\\ accepted' = accepted \\cup {cIntent.merged}",
     "/\\ pAcc' = pAcc /\\ accepted' = accepted"),
    ('the child is recorded before the parent branch is pushed',
     'service.py _integrate() `_push(target_worktree, ...)` before the record', 'child-only',
     'AcceptedIsPushed',
     'cChecked\' = TRUE /\\ Go("child", "cPush")', 'cChecked\' = TRUE /\\ Go("child", "cRecord")'),
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
