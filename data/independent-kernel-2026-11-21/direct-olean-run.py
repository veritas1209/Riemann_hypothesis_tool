#!/usr/bin/env python3
import datetime, hashlib, json, os, pathlib, re, resource, subprocess, time

root = pathlib.Path(__file__).resolve().parent
project = pathlib.Path('/data/lean-projects/remark')
binary = root / 'con-leche/.lake/build/bin/con-leche'
env = dict(os.environ, PATH='/home/user/.elan/bin:' + os.environ['PATH'],
           ELAN_HOME='/home/user/.elan', ELAN_TOOLCHAIN='leanprover/lean4:v4.33.1')
targets = ['riemannHypothesis', 'riemannZeta_eq_zero_re',
           'LFunction_ne_zero_of_half_lt_re', 'LFunction_eq_zero_re',
           'LFunction_eq_zero_re_of_isPrimitive']
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
state = {'status': 'running', 'pid': os.getpid(), 'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
         'scope': 'Five headline theorems and their transitive dependencies, read directly from our compiled .olean files',
         'independent_kernel_recheck_passed': False, 'threads': 32}

def save(**values):
    state.update(values, updated_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
    temp = root / 'direct-olean-status.json.tmp'
    temp.write_text(json.dumps(state, indent=2) + '\n')
    temp.replace(root / 'direct-olean-status.json')

try:
    assert root == pathlib.Path('/data/lean-projects/remark-independent-kernel-20261121')
    main_hash = hashlib.sha256((project / '.lake/build/lib/lean/Main.olean').read_bytes()).hexdigest()
    assert main_hash == '9146dfb4148e24d2252e04c50742f3e49dd126dfcc5006d10bf5f873ddf796ad'
    paths = subprocess.check_output(['/home/user/.elan/bin/lake', 'env', 'printenv', 'LEAN_PATH'], cwd=project, env=env, text=True).strip()
    core = '/home/user/.elan/toolchains/leanprover--lean4---v4.33.1/lib/lean'
    paths = paths + ':' + core
    args = [str(binary), '--verified', '--jobs=32', '--progress=10s', '--olean', '--search-path=' + paths]
    args += ['--constant=' + name for name in targets] + ['Main']
    save(command=args, cwd=str(project), main_olean_sha256=main_hash,
         checker_binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest())
    start = time.monotonic()
    with (root / 'direct-olean-check.log').open('wb') as log:
        p = subprocess.Popen(['/usr/bin/time', '-v', '-o', str(root/'direct-olean-check.time.txt'), *args],
                             cwd=project, env=env, stdout=log, stderr=log)
        while p.poll() is None:
            memory = {line.split(':')[0]: int(line.split()[1])*1024 for line in pathlib.Path('/proc/meminfo').read_text().splitlines() if line.startswith(('MemTotal:', 'MemAvailable:'))}
            save(elapsed_seconds=round(time.monotonic()-start, 1), system_memory=memory)
            time.sleep(10)
        code = p.wait()
    verdict = (root/'direct-olean-check.log').read_text()
    matches = re.findall(r'^con-leche: accepted ([1-9]\d*) declarations \(--verified\)$', verdict, re.M)
    assert code == 0 and len(matches) == 1, f'checker exit {code}; see direct-olean-check.log'
    assert hashlib.sha256((project / '.lake/build/lib/lean/Main.olean').read_bytes()).hexdigest() == main_hash
    save(status='completed', independent_kernel_recheck_passed=True, exit_code=0,
         accepted_declarations=int(matches[0]), elapsed_seconds=round(time.monotonic()-start, 3),
         completed_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
except BaseException as exc:
    save(status='failed', error=repr(exc))
    raise
