"""D10 phase 1 tests for sandbox_bwrap. Run: PYTHONPATH=.:probes:sandbox python3 tests/t_sandbox_bwrap.py"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sandbox_bwrap as sb

from suites.repo import scorers as pr

FAIL = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        FAIL.append(name)


INSTANCE = pr._INSTANCES["REPO_RANGESET_1"]
TDIR = pr._instance_dir(INSTANCE)
CASES = TDIR / "hidden" / "cases.json"
PY = sys.executable


def src_of(base):
    return {str(f.relative_to(base)): f.read_text()
            for f in (base / INSTANCE["pkg"]).rglob("*.py")}


def run(src, sandbox):
    old = os.environ.pop("EVAL_SANDBOX", None)
    if sandbox:
        os.environ["EVAL_SANDBOX"] = "bwrap"
    try:
        return pr.run_hidden(INSTANCE, src, runner_manifest=INSTANCE)
    finally:
        os.environ.pop("EVAL_SANDBOX", None)
        if old is not None:
            os.environ["EVAL_SANDBOX"] = old

# (e) EVAL_SANDBOX=bwrap + bwrap missing -> void sandbox_unavailable
real = sb.available
sb.available = lambda refresh=False: (False, "bwrap_not_installed")
try:
    e = run(src_of(TDIR / "repo"), True)
finally:
    sb.available = real
check("(e) missing bwrap -> sandbox_unavailable", e["infra_error"] == "sandbox_unavailable"
      and not e["f2p"] and not e["p2p"], str(e))

# (f) bwrap present but failing at runtime (fake bwrap=/bin/false) -> void, not FAIL
real_bp = sb.bwrap_path
sb.bwrap_path = lambda: "/bin/false"
try:
    f = run(src_of(TDIR / "repo"), True)
    sb.available(refresh=True)
finally:
    sb.bwrap_path = real_bp
    sb._cache = None
check("(f) failing bwrap -> sandbox_unavailable", f["infra_error"] == "sandbox_unavailable"
      and not f["f2p"] and not f["p2p"], str(f))

# (g) python_ro containment (unit; subprocess mocked to report a given prefix)
import json as _json
import types as _types
_real_run = sb.subprocess.run


def pro(prefix, forbid):
    sb.subprocess.run = lambda *a, **k: _types.SimpleNamespace(
        stdout=_json.dumps([prefix, prefix]) + "\n", returncode=0)
    try:
        return sb.python_ro("py", forbid)
    finally:
        sb.subprocess.run = _real_run


with tempfile.TemporaryDirectory(dir=os.path.expanduser("~")) as hd:
    H = os.path.realpath(os.path.join(hd, "user"))
    os.makedirs(H + "/venv")
    os.makedirs(H + "X/venv")
    outside = tempfile.mkdtemp(dir=os.path.expanduser("~"))
    try:
        os.symlink(H + "/venv", outside + "/link")
        cases = [
            ("prefix == HOME", H, False),
            ("prefix inside HOME", H + "/venv", False),
            ("prefix contains HOME", hd, False),
            ("symlink into HOME", outside + "/link", False),
            ("lookalike HOMEX ok", H + "X/venv", True),
            ("trailing slash HOME", H + "/", False),
            ("trailing slash forbid", H + "/venv", False, [H + "/"]),
            ("under /tmp rejected", "/tmp/somevenv", False),
            ("system prefix skipped", "/usr", True),
        ]
        for c in cases:
            name, pref, ok_exp = c[:3]
            forb = c[3] if len(c) > 3 else [H]
            dirs, why = pro(pref, forb)
            check(f"(g) python_ro {name}", (dirs is not None) == ok_exp, f"{dirs} {why}")
        dirs, _ = pro("/usr", [H])
        check("(g) system prefix -> no extra dirs", dirs == [])
    finally:
        shutil.rmtree(outside, True)

# (h) generated passwd/group: root + current uid only, home=/tmp; argv binds them
d = sb._ident_files()
pw = open(os.path.join(d, "passwd")).read()
check("(h) passwd minimal", pw.startswith("root:x:0:0:") and
      (os.getuid() == 0 or f"sandbox:x:{os.getuid()}:{os.getgid()}:sandbox:/tmp:" in pw)
      and len(pw.splitlines()) <= 2, pw)
argv = sb.wrap(["true"])
check("(h) real /etc/passwd not bound", ["/etc/passwd", "/etc/passwd"] not in
      [argv[i:i + 2] for i in range(len(argv))] and "/etc/group" in argv)
argv = sb.wrap(["true"], extra_ro=["/opt/x"])
i = argv.index("/opt/x")
check("(h) extra_ro strict + after /tmp", argv[i - 1] == "--ro-bind"
      and argv.index("/tmp") < i)

ok, why = sb.available()
if not ok:
    print(f"SKIP rest: bwrap unavailable ({why})")
    print("ALL PASS" if not FAIL else f"FAILED: {FAIL}")
    sys.exit(1 if FAIL else 0)

with tempfile.TemporaryDirectory() as td:
    # (a) file outside binds invisible; writes outside rw fail
    code = ("import sys\ntry:\n open(sys.argv[1]).read(); print('READ')\n"
            "except OSError as e: print('ERR', e.errno)\n"
            "try:\n open(sys.argv[2],'w').write('x'); print('WROTE')\n"
            "except OSError as e: print('WERR', e.errno)\n")
    outside = str(Path.home() / "sbx_should_not_exist")
    r = subprocess.run(sb.wrap([PY, "-c", code, str(CASES), outside], rw=[td], cwd=td),
                       capture_output=True, text=True, timeout=30)
    check("(a) hidden cases.json not readable (ENOENT)", "ERR 2" in r.stdout, r.stdout + r.stderr)
    check("(a) write outside binds fails", "WERR" in r.stdout and not os.path.exists(outside),
          r.stdout)
    rcode = ("import sys\nfor p in sys.argv[1:]:\n try:\n  open(p).read() if not "
             "__import__('os').path.isdir(p) else __import__('os').listdir(p); print('READ', p)\n"
             " except OSError as e: print('ERR', p)\n")
    targets = [str(Path.home() / ".ssh"), str(Path(pr.__file__).resolve().parent),
               str(pr.SCN_ROOT), str(TDIR), "/etc/hostname", "/etc/shadow", "/etc/fstab"]
    r = subprocess.run(sb.wrap([PY, "-I", "-B", "-c", rcode, *targets], rw=[td], cwd=td),
                       capture_output=True, text=True, timeout=30)
    check("(a) python3 -I -B starts", r.returncode == 0, r.stderr)
    for t in targets:
        check(f"(a) not readable: {t}", f"READ {t}\n" not in r.stdout + "\n", r.stdout)
    # (b) no network
    code = ("import socket\ns=socket.socket(); s.settimeout(3)\n"
            "try:\n s.connect(('1.1.1.1',53)); print('CONNECTED')\n"  # sweep-ok (real IP: egress test must target a routable host)
            "except OSError as e: print('NONET', e)\n")
    r = subprocess.run(sb.wrap([PY, "-c", code], rw=[td]), capture_output=True, text=True,
                       timeout=30)
    check("(b) no network", "NONET" in r.stdout, r.stdout + r.stderr)
    # (c) pass_fds pipe works
    rfd, wfd = os.pipe()
    p = subprocess.Popen(sb.wrap([PY, "-c", f"import os; os.write({wfd}, b'hello-pipe')"],
                                 rw=[td]), pass_fds=(wfd,))
    os.close(wfd)
    p.wait(30)
    got = os.read(rfd, 100)
    os.close(rfd)
    check("(c) pass_fds pipe", got == b"hello-pipe", repr(got))
    # (i) inside: generated passwd, uid resolves to 'sandbox'
    r = subprocess.run(sb.wrap([PY, "-I", "-c", "import pwd,os;print(open('/etc/passwd').read());"
                                "print('ME', pwd.getpwuid(os.getuid()).pw_name)"], rw=[td]),
                       capture_output=True, text=True, timeout=30)
    exp = "root" if os.getuid() == 0 else "sandbox"
    check("(i) sandbox passwd generated", f"ME {exp}" in r.stdout
          and len(r.stdout.strip().splitlines()) <= 4, r.stdout + r.stderr)


# (d) run_hidden sandboxed == unsandboxed on reference / buggy
for label, base in (("reference", TDIR / "reference"), ("buggy", TDIR / "repo")):
    s = src_of(TDIR / "repo")
    if label == "reference":
        s.update(src_of(base))        # reference ships only the fixed files
    u, w = run(s, False), run(s, True)
    check(f"(d) {label} valid both", u["infra_error"] is None and w["infra_error"] is None,
          f"{u['infra_error']} | {w['infra_error']} | {w.get('tail','')[-300:]}")
    check(f"(d) {label} same results", (u["f2p"], u["p2p"]) == (w["f2p"], w["p2p"]),
          f"{u['f2p']} {w['f2p']}")
    check(f"(d) {label} sandbox recorded", w.get("sandbox") == "bwrap"
          and w.get("network_isolated") is True and "sandbox" not in u)
    if label == "reference":
        check("(d) reference all pass", all(w["f2p"].values()) and all(w["p2p"].values()))
    else:
        check("(d) buggy fails f2p", not all(w["f2p"].values()))

print("ALL PASS" if not FAIL else f"FAILED: {FAIL}")
sys.exit(1 if FAIL else 0)
