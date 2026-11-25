"""The optional LibreOffice runtime for legacy Office conversion, and the sandbox it always runs in.

Legacy ``.doc``/``.xls``/``.ppt``/``.vsd`` files are untrusted bytes handed to a large, network-capable
office suite, so the runtime is opt-in, found by configuration rather than imported, and every run is
confined:

* the runtime is ``LIBRARIAN_SOFFICE`` (a ``soffice`` path), else the user-space install under
  ``tools/extraction/.local/libreoffice/current`` made by ``provision_libreoffice.py`` (no root), else
  a ``soffice`` on ``PATH`` (a system package); with none of them, files stay ``needs-converter-runtime``;
* each conversion gets a throwaway work directory: a read-only copy of the verified input, an output
  directory, a private profile with macros, links and updates switched off, and a private temp/HOME;
* the child runs without network, with a scrubbed environment, resource limits, a wall-clock timeout
  and a tree kill, inside ``bwrap`` (own mount namespace: only the runtime and system libraries are
  visible, the source tree and the rest of the home directory are not), else ``unshare`` (no network
  and a private PID namespace, but the host filesystem stays visible), else not at all:
  ``LIBRARIAN_CONVERTER_SANDBOX`` (``auto``, ``bwrap``, ``unshare``, ``none``) forces a mode, and ``none``
  is the only way to run unconfined, so it is an explicit operator decision recorded in each rendition.

Standard library only.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import uuid

from tools.extraction import bounded

TOOLKIT = Path(__file__).resolve().parents[2]
LOCAL = Path("tools/extraction/.local/libreoffice")
RUNTIME_ENV = "LIBRARIAN_SOFFICE"
SANDBOX_ENV = "LIBRARIAN_CONVERTER_SANDBOX"
MODES = ("bwrap", "unshare", "none")
NEEDS_RUNTIME = "needs-converter-runtime"
NEEDS_SANDBOX = "needs-converter-sandbox"
WALL_SECONDS = 300
PROBE_SECONDS = 30
VERSION_SECONDS = 60
MAX_STDOUT = 64 * 1024
TMP_BYTES = 512 * 1024 * 1024  # the private temp tmpfs (and an eighth of it for each of /tmp and /var/tmp)
# Applied to the whole sandbox (``bwrap`` and every descendant inherit them). CPU is set per run.
LIMITS = {"RLIMIT_AS": 6 * 1024 ** 3, "RLIMIT_FSIZE": 128 * 1024 ** 2, "RLIMIT_NOFILE": 1024, "RLIMIT_CORE": 0}
CPU_SLACK = 30
PROVENANCE = "provenance.json"
# Settings written into the throwaway profile: no macro execution or active content, no link, field or
# chart updates from a document (the network is unreachable anyway; this keeps the intent explicit).
PROFILE_SETTINGS = (
    ("/org.openoffice.Office.Common/Security/Scripting", "MacroSecurityLevel", "3"),
    ("/org.openoffice.Office.Common/Security/Scripting", "DisableMacrosExecution", "true"),
    ("/org.openoffice.Office.Common/Security/Scripting", "BlockUntrustedRefererLinks", "true"),
    ("/org.openoffice.Office.Writer/Content/Update", "Link", "0"),
    ("/org.openoffice.Office.Writer/Content/Update", "Field", "false"),
    ("/org.openoffice.Office.Writer/Content/Update", "Chart", "false"),
    ("/org.openoffice.Office.Calc/Content/Update", "Link", "0"),
)
# Raises the limits, then replaces itself with the command: run with ``python -I -S -c``.
LIMIT_SHIM = (
    "import os, resource, sys\n"
    "args = sys.argv[1:]\n"
    "split = args.index('--')\n"
    "for item in args[:split]:\n"
    "    name, value = item.split('=')\n"
    "    resource.setrlimit(getattr(resource, name), (int(value), int(value)))\n"
    "os.execvp(args[split + 1], args[split + 1:])\n"
)
# Directories a dynamically linked runtime needs from the host, read-only. Symlinks (merged /usr) are recreated.
SYSTEM_DIRS = ("bin", "sbin", "lib", "lib32", "lib64")
SYSTEM_FILES = ("/etc/ld.so.cache",)


def _executable(path):
    return path.is_file() and os.access(path, os.X_OK)


def locate(system=True):
    """The ``soffice`` this instance would use, or None. An explicit setting is never replaced by a fallback.

    ``system=False`` ignores a ``soffice`` on ``PATH`` (provisioned or configured runtimes only).
    """
    configured = os.environ.get(RUNTIME_ENV)
    if configured:
        path = Path(os.path.abspath(configured))
        return path if _executable(path) else None
    local = TOOLKIT / LOCAL / "current" / "program" / "soffice"
    if _executable(local):
        return local
    found = shutil.which("soffice") if system else None
    return Path(found) if found else None


def runtime_root(executable):
    """``(real soffice path, directory bound read-only into the sandbox)``.

    ``<root>/program/soffice`` binds ``<root>``; any other layout binds only the directory that holds it.
    """
    real = Path(os.path.realpath(executable))
    return real, (real.parent.parent if real.parent.name == "program" else real.parent)


def provenance(executable):
    """The install record the provisioning script wrote next to the runtime, or None."""
    try:
        record = json.loads((runtime_root(executable)[1] / PROVENANCE).read_text())
        return {key: record[key] for key in ("version", "archive_sha256")}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _tools_present(names):
    return all(shutil.which(name) for name in names)


def _view(mode, work):
    """The paths the child sees: fixed mount points under ``bwrap``, the real directories otherwise."""
    if mode == "bwrap":
        return {"in": "/work/in", "out": "/work/out", "profile": "/work/profile", "tmp": "/work/tmp"}
    return {"in": str(work / "in"), "out": str(work / "out"), "profile": str(work / "profile"),
            "tmp": str(work / "tmp")}


def _environment(mode, work):
    tmp = _view(mode, work)["tmp"]
    return {"PATH": "/usr/bin:/bin", "HOME": tmp + "/home", "TMPDIR": tmp, "TZ": "UTC", "LC_ALL": "C.UTF-8",
            "LANG": "C.UTF-8", "SAL_USE_VCLPLUGIN": "svp", "SAL_DISABLE_OPENCL": "1"}


def _bwrap(root, work):
    argv = [shutil.which("bwrap"), "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL", "--clearenv"]
    for name in SYSTEM_DIRS:
        path = Path("/" + name)
        if path.is_symlink():
            argv += ["--symlink", os.readlink(path), str(path)]
        elif path.is_dir():
            argv += ["--ro-bind", str(path), str(path)]
    argv += ["--ro-bind", "/usr", "/usr"]
    # LibreOffice's launcher needs a writable /tmp or /var/tmp for its IPC pipe, or it exits. These come first so a
    # runtime installed under /tmp (bound below) is not hidden by them.
    argv += ["--size", str(TMP_BYTES // 8), "--tmpfs", "/tmp", "--size", str(TMP_BYTES // 8), "--tmpfs", "/var/tmp"]
    for name in SYSTEM_FILES:
        argv += ["--ro-bind-try", name, name]
    argv += ["--ro-bind-try", "/etc/fonts", "/etc/fonts"]
    if root is not None:
        argv += ["--ro-bind", str(root), str(root)]
    argv += ["--ro-bind", str(work / "in"), "/work/in", "--bind", str(work / "out"), "/work/out",
             "--bind", str(work / "profile"), "/work/profile",
             "--size", str(TMP_BYTES), "--tmpfs", "/work/tmp", "--dir", "/work/tmp/home",

             "--proc", "/proc", "--dev", "/dev", "--chdir", "/work/tmp"]
    for key, value in _environment("bwrap", work).items():
        argv += ["--setenv", key, value]
    return argv + ["--"]


_UNSHARE = {}


def _unshare_map():
    """The user-namespace mapping flags. The caller's own uid keeps the kernel's read-only checks (a mapped
    root would override file permissions); older util-linux without ``--map-user`` falls back to root."""
    return _UNSHARE.get("map") or ["--map-root-user"]


def _command(mode, root, work, inner, timeout):
    """The full argv: limit shim, then the sandbox launcher, then ``inner``."""
    limits = [name + "=" + str(value) for name, value in {**LIMITS, "RLIMIT_CPU": int(timeout) + CPU_SLACK}.items()]
    shim = [sys.executable, "-I", "-S", "-c", LIMIT_SHIM, *limits, "--"]
    if mode == "bwrap":
        return shim + _bwrap(root, work) + inner
    if mode == "unshare":
        return shim + [shutil.which("unshare"), "--user", *_unshare_map(), "--net", "--pid", "--fork",
                       "--kill-child", "--"] + inner
    return shim + inner


def _workdir(parent):
    work = Path(parent) / ("convert-" + uuid.uuid4().hex)
    os.mkdir(work, 0o700)
    for name in ("in", "out", "profile", "tmp", "tmp/home"):
        os.mkdir(work / name, 0o700)
    return work


def _run(mode, executable, work, arguments, timeout, diagnostics=None):
    """Run ``arguments`` (after the runtime's ``soffice`` when given) confined in ``mode``; ``(code, stdout)``."""
    real, root = runtime_root(executable) if executable is not None else (None, None)
    inner = ([str(real)] if real is not None else []) + arguments
    command = _command(mode, root, work, inner, timeout)
    environment = {"PATH": "/usr/bin:/bin"} if mode == "bwrap" else _environment(mode, work)
    # Never the toolkit's directory: the child's relative paths must land in the throwaway directory.
    return bounded.run(command, timeout, MAX_STDOUT, env=environment, stderr_limit=MAX_STDOUT,
                       cwd=str(work / "tmp"), diagnostics=diagnostics)


_MODE = {}


def sandbox_mode():
    """The confinement mode in force (``bwrap``, ``unshare`` or an explicit ``none``), or None when the host has none.

    Probed once per process: a trivial command must run inside it.
    """
    wanted = os.environ.get(SANDBOX_ENV, "auto").strip().lower() or "auto"
    if wanted not in ("auto", *MODES):
        return None
    if wanted in _MODE:
        return _MODE[wanted]
    found = None
    if wanted == "none":
        found = "none"
    else:
        for mode in (MODES[:2] if wanted == "auto" else (wanted,)):
            if not _tools_present((("bwrap",) if mode == "bwrap" else ("unshare",))):
                continue
            maps = [["--map-user=%d" % os.getuid(), "--map-group=%d" % os.getgid()], ["--map-root-user"]]
            for choice in (maps if mode == "unshare" else [None]):
                _UNSHARE["map"] = choice
                if _probe(mode):
                    found = mode
                    break
            if found:
                break
    _MODE[wanted] = found
    return found


def _probe(mode):
    with tempfile.TemporaryDirectory(prefix="librarian-sandbox-") as parent:
        work = _workdir(parent)
        try:
            code, _ = _run(mode, None, work, ["true"], PROBE_SECONDS)
        except bounded.BoundedError:
            return False
        return code == 0


def reset():
    """Forget the cached sandbox probe and runtime identity (tests change the environment)."""
    _MODE.clear()
    _IDENTITY.clear()
    _UNSHARE.clear()


def refusal():
    """``None`` when a conversion can start, else the stable code that explains why not."""
    if locate() is None:
        return NEEDS_RUNTIME
    return None if sandbox_mode() is not None else NEEDS_SANDBOX


def version(executable, mode):
    """The first line ``soffice --version`` prints, run under the same confinement as a conversion."""
    with tempfile.TemporaryDirectory(prefix="librarian-soffice-") as parent:
        work = _workdir(parent)
        code, out = _run(mode, executable, work, ["--version"], VERSION_SECONDS)
    text = out.decode("utf-8", "replace").strip()[:200]
    if code != 0 or not text:
        raise bounded.BoundedError("converter-runtime-failed", "soffice --version failed")
    return text


_IDENTITY = {}


def identity():
    """Which runtime and confinement produce a rendition: version, sandbox mode, install record."""
    executable = locate()
    if executable is None:
        raise bounded.BoundedError(NEEDS_RUNTIME, "Legacy Office conversion needs LibreOffice (soffice)")
    mode = sandbox_mode()
    if mode is None:
        raise bounded.BoundedError(NEEDS_SANDBOX, "No sandbox (bwrap or unshare) is available for LibreOffice")
    info = os.stat(os.path.realpath(executable))
    key = (str(executable), info.st_ino, info.st_size, info.st_mtime_ns, mode)
    if key not in _IDENTITY:
        found = {"soffice": version(executable, mode), "sandbox": mode, "distribution": provenance(executable)}
        _IDENTITY.clear()
        _IDENTITY[key] = found
    return dict(_IDENTITY[key])


def status():
    """Informational readiness report (never an error): what a legacy file would do right now."""
    executable = locate()
    mode = sandbox_mode()
    report = {"provisioned": executable is not None, "executable": str(executable) if executable else None,
              "sandbox": mode, "ready": False, "code": None, "action": None}
    if executable is None:
        report.update(code=NEEDS_RUNTIME, action=(
            "Install the pinned user-space runtime with python -m tools.extraction.provision_libreoffice install, or "
            "set " + RUNTIME_ENV + " to a soffice; legacy .doc/.xls/.ppt/.vsd files stay needs-reader until then."))
    elif mode is None:
        report.update(code=NEEDS_SANDBOX, action=(
            "Install bubblewrap (bwrap) or enable unprivileged user namespaces for unshare; setting "
            + SANDBOX_ENV + "=none runs LibreOffice unconfined and is an explicit operator decision."))
    else:
        try:
            report.update(ready=True, runtime=identity())
        except bounded.BoundedError as error:
            report.update(code=error.code, action=str(error))
    return report


def _seed_profile(profile):
    user = profile / "user"
    os.mkdir(user, 0o700)
    items = "".join(
        '<item oor:path="%s"><prop oor:name="%s" oor:op="fuse"><value>%s</value></prop></item>\n'
        % (path, name, value) for path, name, value in PROFILE_SETTINGS)
    (user / "registrymodifications.xcu").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<oor:items xmlns:oor="http://openoffice.org/2001/registry" '
        'xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
        + items + "</oor:items>\n")


@contextmanager
def exported(parent, raw, suffix, target, timeout=WALL_SECONDS):
    """Convert verified bytes ``raw`` of a legacy file to ``target`` format; yield the output path.

    The work directory under ``parent`` is removed on exit. Raises ``bounded.BoundedError`` with
    ``needs-converter-runtime``, ``needs-converter-sandbox``, ``conversion-timeout``, ``conversion-limit``,
    ``legacy-conversion-crashed`` or ``legacy-conversion-failed``.
    """
    executable = locate()
    if executable is None:
        raise bounded.BoundedError(NEEDS_RUNTIME, "Legacy Office conversion needs LibreOffice (soffice)")
    mode = sandbox_mode()
    if mode is None:
        raise bounded.BoundedError(NEEDS_SANDBOX, "No sandbox (bwrap or unshare) is available for LibreOffice")
    work = _workdir(parent)
    try:
        _seed_profile(work / "profile")
        source = work / "in" / ("source" + suffix)
        source.write_bytes(raw)
        os.chmod(source, 0o444)
        os.chmod(work / "in", 0o555)
        view = _view(mode, work)
        errors = []
        code, _ = _run(mode, executable, work, [
            "--headless", "--norestore", "--nologo", "--nodefault", "--nolockcheck",
            "-env:UserInstallation=file://" + view["profile"],
            "--convert-to", target, "--outdir", view["out"], view["in"] + "/source" + suffix], timeout,
            errors)
        detail = b"".join(errors).decode("utf-8", "replace")[-2000:]
        produced = work / "out" / ("source." + target)
        if code < 0 or 128 < code < 160:  # killed by a signal (the launchers report 128 + signal)
            raise bounded.BoundedError("legacy-conversion-crashed", "LibreOffice was killed by a signal", detail)
        if code != 0 or not produced.is_file() or produced.is_symlink():
            raise bounded.BoundedError("legacy-conversion-failed", "LibreOffice did not convert the legacy document",
                                       detail)
        yield produced
    finally:
        os.chmod(work / "in", 0o700)
        shutil.rmtree(work, ignore_errors=True)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=["status"])
    parser.parse_args(argv)
    print(json.dumps(status(), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
