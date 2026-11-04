"""Model-check the run lease protocol of leases.py with TLC.

Exit 0 when every configuration ends as EXPECTED (and, with --mutants, every
mutant breaks the rule); 1 otherwise; 2 when TLC cannot run.
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
TLC_VERSION = 'TLC2 Version 2.19 of 08 August 2024'
RULE = 'NoMutationUnderAnotherSession'

# None: every invariant holds. A rule name: TLC reports that rule as violated.
EXPECTED = {
    'protocol': None,
    'release-after-death-windows': None,
    'release-after-death-linux': RULE,      # issue: child not yet in the receipt
    'release-any-time-windows': RULE,       # issue: release judges a stale record
}

# (name, code it stands for, configuration, model text, broken text)
MUTANTS = [
    ('run skips the mutation.lock probe', 'leases.py run() register block', 'protocol',
     'IF mut = None\n       THEN pc\' = [pc EXCEPT ![s] = "share"]',
     'IF TRUE\n       THEN pc\' = [pc EXCEPT ![s] = "share"]'),
    ('guard ignores unreleased receipts', 'leases.py guard() `if uses: raise`', 'protocol',
     'IF shared = {} /\\ excl = None /\\ seen = {}', 'IF shared = {} /\\ excl = None'),
    ('guard admits its own use beside others', 'leases.py guard() `len(uses) == 1`', 'protocol',
     'OwnUse(s) == Receipts = {s} /\\ sup[s]', 'OwnUse(s) == s \\in Receipts /\\ sup[s]'),
    ('run removes its receipt while descendants run', 'leases.py run() descendants wait', 'protocol',
     'pc[s] = "wait" /\\ sup[s] /\\ ~child[s]', 'pc[s] = "wait" /\\ sup[s]'),
    ('release skips the child and job checks', 'leases.py release() child/group/job checks',
     'release-after-death-windows',
     'IF sup[s] \\/ (relPick[2] = "tracked" /\\ child[s])', 'IF sup[s]'),
]


def tlc(java, jar, directory, config):
    with tempfile.TemporaryDirectory() as temp:
        proc = subprocess.run(
            [java, '-XX:+UseParallelGC', '-Djava.io.tmpdir=' + temp, '-cp', jar, 'tlc2.TLC',
             '-workers', '1', '-metadir', str(Path(temp) / 'meta'),
             '-config', config + '.cfg', 'RunLeases.tla'],
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
    parser.add_argument('--mutants', action='store_true', help='also check that each mutant breaks the rule')
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
        for name, code, config, old, new in MUTANTS:
            with tempfile.TemporaryDirectory() as temp:
                copy = Path(temp)
                for path in MODEL.iterdir():
                    shutil.copy(path, copy)
                spec = copy / 'RunLeases.tla'
                text = spec.read_text(encoding='utf-8')
                if text.count(old) != 1:
                    print(json.dumps({'mutant': name, 'error': 'model text not found once; update MUTANTS'}))
                    ok = False
                    continue
                spec.write_bytes(text.replace(old, new).encode('utf-8'))
                result = tlc(args.java, args.tla2tools, copy, config)
            caught = result['violated'] == RULE
            ok &= caught
            print(json.dumps({'mutant': name, 'code': code, 'config': config, 'caught': caught, **result}))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
