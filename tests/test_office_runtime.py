"""The sandboxed LibreOffice runtime (Kata qr1d): lookup, confinement, limits, failure codes and provisioning.

Everything but ``RealRuntimeTests`` uses a fake ``soffice`` shell script and synthetic files, so CI needs no
LibreOffice. The confinement tests run the real ``bwrap`` and ``unshare`` launchers (CI installs
bubblewrap and allows unprivileged namespaces), and ``RealRuntimeTests`` runs a provisioned LibreOffice.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import unittest.mock

from tools.extraction import bounded, convert, office_runtime as runtime, provision_libreoffice as provision

MODES = [mode for mode in ("bwrap", "unshare") if shutil.which(mode)]


def usable(mode):
    with unittest.mock.patch.dict(os.environ, {runtime.SANDBOX_ENV: mode}):
        runtime.reset()
        try:
            return runtime.sandbox_mode() == mode
        finally:
            runtime.reset()


BWRAP = usable("bwrap")
UNSHARE = usable("unshare")


def fake_runtime(directory, body):
    """A ``soffice`` script under ``directory``; ``--version`` prints a fixed line, else ``body`` runs.

    ``$IN``/``$OUT`` are the input file and output directory of a conversion, ``$TARGET`` its target format.
    """
    script = Path(directory) / "soffice"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "LibreOffice 9.9 fake"; exit 0; fi\n'
        'while [ $# -gt 0 ]; do case "$1" in --outdir) OUT="$2"; shift;; --convert-to) TARGET="$2"; shift;; '
        '-env:UserInstallation=*) PROFILE="${1#-env:UserInstallation=file://}";; *) IN="$1";; esac; shift; done\n'
        + body)
    script.chmod(stat.S_IRWXU)
    return script


class Case(unittest.TestCase):
    mode = None

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        (self.base / "bin").mkdir()
        self.work = self.base / "work"
        self.work.mkdir()
        patch = unittest.mock.patch.dict(os.environ, {runtime.SANDBOX_ENV: self.mode or "auto"})
        patch.start()
        self.addCleanup(patch.stop)
        runtime.reset()
        self.addCleanup(runtime.reset)

    def fake(self, body):
        script = fake_runtime(self.base / "bin", body)
        patch = unittest.mock.patch.dict(os.environ, {runtime.RUNTIME_ENV: str(script)})
        patch.start()
        self.addCleanup(patch.stop)
        runtime.reset()
        return script

    def export(self, raw=b"\xd0\xcf\x11\xe0 synthetic", suffix=".doc", target="docx", timeout=60):
        with runtime.exported(self.work, raw, suffix, target, timeout) as produced:
            return produced.read_bytes()

    def code(self, **arguments):
        with self.assertRaises(bounded.BoundedError) as caught:
            self.export(**arguments)
        return caught.exception.code

    def assertClean(self):
        self.assertEqual(list(self.work.iterdir()), [], "the throwaway work directory was not removed")


class LocateTests(Case):
    def test_an_explicit_setting_is_never_replaced_by_a_fallback(self):
        missing = self.base / "nope" / "soffice"
        system = fake_runtime(self.base / "bin", "exit 0\n")
        with unittest.mock.patch.dict(os.environ, {runtime.RUNTIME_ENV: str(missing),
                                                   "PATH": str(system.parent) + os.pathsep + os.environ["PATH"]}):
            self.assertIsNone(runtime.locate())
        with unittest.mock.patch.dict(os.environ, {runtime.RUNTIME_ENV: str(system)}):
            self.assertEqual(runtime.locate(), system)

    def test_the_user_space_install_comes_before_the_system_package(self):
        toolkit = self.base / "toolkit"
        local = toolkit / runtime.LOCAL / "current" / "program"
        local.mkdir(parents=True)
        installed = fake_runtime(local, "exit 0\n")
        system = fake_runtime(self.base / "bin", "exit 0\n")
        environment = {"PATH": str(system.parent) + os.pathsep + os.environ["PATH"]}
        with unittest.mock.patch.dict(os.environ, environment), \
                unittest.mock.patch.object(runtime, "TOOLKIT", toolkit):
            os.environ.pop(runtime.RUNTIME_ENV, None)
            self.assertEqual(runtime.locate(), installed)
            installed.unlink()
            self.assertEqual(runtime.locate(), system)
            self.assertIsNone(runtime.locate(system=False))

    def test_no_runtime_is_a_disposition_not_an_error(self):
        with unittest.mock.patch.object(runtime, "locate", return_value=None):
            self.assertEqual(runtime.refusal(), "needs-converter-runtime")
            report = runtime.status()
            self.assertEqual((report["provisioned"], report["ready"], report["code"]), (False, False,
                                                                                        "needs-converter-runtime"))
            self.assertIn("provision_libreoffice", report["action"])
            self.assertEqual(self.code_of(lambda: runtime.identity()), "needs-converter-runtime")
        with unittest.mock.patch.object(runtime, "locate", return_value=None):
            with self.assertRaises(bounded.BoundedError) as caught:
                self.export()
        self.assertEqual(caught.exception.code, "needs-converter-runtime")

    def code_of(self, call):
        with self.assertRaises(bounded.BoundedError) as caught:
            call()
        return caught.exception.code

    def test_a_host_without_a_sandbox_refuses_with_its_own_code_unless_the_operator_opts_out(self):
        self.fake("exit 0\n")
        with unittest.mock.patch.object(runtime, "sandbox_mode", return_value=None):
            self.assertEqual(runtime.refusal(), "needs-converter-sandbox")
            self.assertEqual(runtime.status()["code"], "needs-converter-sandbox")
            self.assertEqual(self.code(), "needs-converter-sandbox")
        with unittest.mock.patch.dict(os.environ, {runtime.SANDBOX_ENV: "none"}):
            runtime.reset()
            self.assertEqual(runtime.sandbox_mode(), "none")
            self.assertEqual(runtime.identity()["sandbox"], "none")
        with unittest.mock.patch.dict(os.environ, {runtime.SANDBOX_ENV: "bogus"}):
            runtime.reset()
            self.assertIsNone(runtime.sandbox_mode())


class ConfinementChecks:
    """Behaviour every confined mode shares; ``mode`` selects the launcher."""

    def test_a_conversion_sees_a_read_only_input_a_private_profile_and_no_network(self):
        self.fake(
            'report="$OUT/report.txt"\n'
            '{ echo "target=$TARGET"; echo "profile=$(ls "$PROFILE/user")";\n'
            '  (: > "$IN.more") 2>/dev/null && echo input-dir-writable || echo input-dir-readonly;\n'
            '  (echo x > "$IN") 2>/dev/null && echo input-writable || echo input-readonly;\n'
            '  echo "interfaces=$(cut -d: -f1 /proc/net/dev | tail -n +3 | tr -d " " | tr "\\n" ",")";\n'
            '  echo "env=$(env | grep -c "^LIBRARIAN\\|^SECRET")";\n'
            '  echo "cwd-writable=$( (: > ./probe) 2>/dev/null && echo yes || echo no)";\n'
            '  echo "tmp-writable=$( (: > "$TMPDIR/probe") 2>/dev/null && echo yes || echo no)";\n'
            '  echo "tree=$(ls "%s" 2>/dev/null | wc -l)"; } > "$report"\n'
            'cp "$report" "$OUT/source.$TARGET"\n' % self.base)
        with unittest.mock.patch.dict(os.environ, {"SECRET_TOKEN": "x", "LIBRARIAN_PRIVATE": "y"}):
            lines = dict(line.split("=", 1) if "=" in line else (line, "")
                         for line in self.export().decode().splitlines())
        self.assertEqual(lines["target"], "docx")
        self.assertEqual(lines["profile"], "registrymodifications.xcu")
        self.assertIn("input-readonly", lines)
        self.assertIn("input-dir-readonly", lines)
        self.assertEqual(lines["interfaces"], "lo,")
        self.assertEqual(lines["env"], "0")
        self.assertEqual(lines["tmp-writable"], "yes")
        self.assertEqual(lines["cwd-writable"], "yes")
        self.assertFalse((Path.cwd() / "probe").exists(), "a relative write escaped into the toolkit directory")
        self.assertClean()

    def test_the_profile_switches_macros_and_updates_off(self):
        self.fake('cp "${PROFILE}/user/registrymodifications.xcu" "$OUT/source.$TARGET"\n')
        xcu = self.export().decode()
        for name in ("MacroSecurityLevel", "DisableMacrosExecution", "BlockUntrustedRefererLinks"):
            self.assertIn(name, xcu)
        self.assertIn("<value>3</value>", xcu)

    def test_a_hang_is_killed_with_its_whole_process_tree(self):
        token = "47.%06d" % (os.getpid() % 1000000)
        self.fake('sleep %s &\nsleep %s\n' % (token, token))
        started = time.monotonic()
        self.assertEqual(self.code(timeout=2), "conversion-timeout")
        self.assertLess(time.monotonic() - started, 30)
        deadline = time.monotonic() + 10
        while self.tree_running(token) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(self.tree_running(token), "a descendant outlived the killed conversion")
        self.assertClean()

    @staticmethod
    def tree_running(token):
        for entry in Path("/proc").iterdir():
            try:
                if (entry / "cmdline").read_bytes().split(b"\0")[:2] == [b"sleep", token.encode()]:
                    return True
            except OSError:
                continue
        return False

    def test_a_crash_is_its_own_code(self):
        self.fake("sh -c 'kill -SEGV $$'\nexit $?\n")  # not PID 1, which ignores signals it does not handle
        self.assertEqual(self.code(), "legacy-conversion-crashed")
        self.assertClean()

    def test_failures_are_coded_and_clean_up(self):
        self.fake("echo error >&2\nexit 3\n")
        self.assertEqual(self.code(), "legacy-conversion-failed")
        self.fake("exit 0\n")  # exits cleanly but produces nothing
        self.assertEqual(self.code(), "legacy-conversion-failed")
        self.assertClean()

    def test_runaway_output_is_stopped_by_the_file_size_limit(self):
        self.fake('dd if=/dev/zero of="$OUT/source.$TARGET" bs=1M count=400 2>/dev/null\n')
        with unittest.mock.patch.dict(runtime.LIMITS, {"RLIMIT_FSIZE": 4 * 1024 * 1024}):
            self.assertIn(self.code(), ("legacy-conversion-crashed", "legacy-conversion-failed"))
        self.assertClean()

    def test_the_runtime_identity_names_version_and_sandbox(self):
        self.fake("exit 0\n")
        found = runtime.identity()
        self.assertEqual((found["soffice"], found["sandbox"]), ("LibreOffice 9.9 fake", self.mode))
        self.assertIsNone(found["distribution"])
        self.assertEqual(runtime.status()["ready"], True)


@unittest.skipUnless(BWRAP, "bubblewrap unavailable")
class BwrapTests(ConfinementChecks, Case):
    mode = "bwrap"

    def test_the_source_tree_and_home_directory_are_not_visible(self):
        marker = self.base / "private.txt"
        marker.write_text("customer data")
        self.fake('echo "x=$(ls "%s" 2>&1 | wc -l)" > "$OUT/source.$TARGET"\n'
                  'echo "home=$(ls "%s" 2>/dev/null | wc -l)" >> "$OUT/source.$TARGET"\n'
                  % (marker.parent, Path.home()))
        text = self.export().decode()
        self.assertIn("home=0", text)
        self.assertIn("x=1", text)  # the error line only: the directory does not exist inside the sandbox

    def test_the_runtime_directory_itself_is_read_only(self):
        script = self.fake('(echo x > "%s/marker") 2>/dev/null && echo writable > "$OUT/source.$TARGET" '
                           '|| echo readonly > "$OUT/source.$TARGET"\n' % (self.base / "bin"))
        self.assertEqual(self.export().decode().strip(), "readonly")
        self.assertFalse((script.parent / "marker").exists())


@unittest.skipUnless(UNSHARE, "unprivileged user and network namespaces unavailable")
class UnshareTests(ConfinementChecks, Case):
    mode = "unshare"


class LimitShimTests(unittest.TestCase):
    def test_resource_limits_apply_to_the_launched_command(self):
        command = [sys.executable, "-I", "-S", "-c", runtime.LIMIT_SHIM,
                   "RLIMIT_NOFILE=17", "RLIMIT_CORE=0", "--", sys.executable, "-c",
                   "import resource; print(resource.getrlimit(resource.RLIMIT_NOFILE)[0], "
                   "resource.getrlimit(resource.RLIMIT_CORE)[0])"]
        done = subprocess.run(command, capture_output=True, text=True, check=True)
        self.assertEqual(done.stdout.split(), ["17", "0"])


class ConversionFailureTests(unittest.TestCase):
    """A failing legacy conversion is a coded, per-file refusal that never ends the run or repeats."""

    def test_every_runtime_failure_becomes_a_conversion_error_with_its_code(self):
        for code in ("needs-converter-runtime", "needs-converter-sandbox", "conversion-timeout",
                     "legacy-conversion-crashed", "legacy-conversion-failed", "converter-runtime-failed"):
            with self.subTest(code=code):
                def fail(*_args, code=code, **_kwargs):
                    raise bounded.BoundedError(code, "refused")
                with tempfile.TemporaryDirectory() as home, unittest.mock.patch.object(
                        runtime, "exported", side_effect=fail):
                    root = Path(home)
                    path = root / "old.doc"
                    path.write_bytes(convert.OLE + b"\0" * 64)
                    with unittest.mock.patch.object(convert.catalog, "private_directory", return_value=root):
                        with self.assertRaises(convert.ConversionError) as caught:
                            convert._legacy(root, path, hashlib.sha256(path.read_bytes()).hexdigest())
                self.assertEqual(caught.exception.code, code)


def build_deb(members, name="data.tar.xz"):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:xz") as archive:
        for path, content in members.items():
            info = tarfile.TarInfo(path)
            if content is None:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)
            else:
                info.size = len(content)
                info.mode = 0o755
                archive.addfile(info, io.BytesIO(content))
    body = data.getvalue()
    out = b"!<arch>\n"
    for member, payload in (("debian-binary", b"2.0\n"), (name, body)):
        out += member.ljust(16).encode() + b"0".ljust(12) + b"0".ljust(6) + b"0".ljust(6) + b"100644".ljust(8) \
            + str(len(payload)).ljust(10).encode() + b"`\n" + payload + (b"\n" if len(payload) % 2 else b"")
    return out


def build_bundle(path, debs):
    with tarfile.open(path, "w:gz") as bundle:
        for name, raw in debs.items():
            info = tarfile.TarInfo("Bundle/DEBS/" + name)
            info.size = len(raw)
            bundle.addfile(info, io.BytesIO(raw))


PREFIX = "./opt/libreoffice9.9/"


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.local = self.base / "local"
        script = b"#!/bin/sh\necho LibreOffice 9.9\n"
        self.debs = {
            "libobasis9.9-core_9.9.0-1_amd64.deb": build_deb({
                "./opt/": None, PREFIX: None, PREFIX + "program/": None, PREFIX + "program/soffice": script,
                PREFIX + "program/libx.so": b"\0" * 10}),
            "libobasis9.9-writer_9.9.0-1_amd64.deb": build_deb({PREFIX + "share/writer.xcd": b"<x/>"}),
            "libobasis9.9-extension-nlpsolver_9.9.0-1_amd64.deb": build_deb({PREFIX + "share/solver": b"no"}),
        }
        self.archive = self.base / "bundle.tar.gz"
        build_bundle(self.archive, self.debs)
        raw = self.archive.read_bytes()
        self.pins = {"version": "9.9.0.1", "series": "9.9", "bytes": len(raw),
                     "sha256": hashlib.sha256(raw).hexdigest(), "url": "https://example.invalid/b.tgz",
                     "archive_name": "b.tgz", "architecture": "x86_64"}

    def test_a_verified_bundle_unpacks_only_the_needed_packages_without_root(self):
        target = provision.install_archive(self.archive, self.pins, self.local)
        self.assertEqual(target, self.local / "9.9.0.1")
        self.assertTrue(os.access(target / "program" / "soffice", os.X_OK))
        self.assertTrue((target / "share" / "writer.xcd").is_file())
        self.assertFalse((target / "share" / "solver").exists())
        self.assertEqual(os.readlink(self.local / "current"), "9.9.0.1")
        record = json.loads((target / "provenance.json").read_text())
        self.assertEqual((record["version"], record["archive_sha256"]),
                         ("LibreOffice 9.9.0.1", self.pins["sha256"]))
        self.assertEqual([p for p in self.local.iterdir() if p.name.startswith(".")], [])
        # The install is found where the toolkit looks for it, and its record becomes part of the runtime identity.
        self.assertEqual(runtime.provenance(self.local / "current" / "program" / "soffice"),
                         {"version": "LibreOffice 9.9.0.1", "archive_sha256": self.pins["sha256"]})
        with self.assertRaises(provision.ProvisionError):
            provision.install_archive(self.archive, self.pins, self.local)

    def test_an_archive_that_differs_from_the_pins_is_never_unpacked(self):
        for change in ({"sha256": "0" * 64}, {"bytes": self.pins["bytes"] + 1}, {"bytes": 10}):
            with self.subTest(change=change), self.assertRaises(provision.ProvisionError):
                provision.install_archive(self.archive, {**self.pins, **change}, self.local)
            self.assertFalse(self.local.exists() and any(self.local.iterdir()))

    def test_members_outside_the_install_prefix_or_of_unexpected_type_are_refused(self):
        for name, members in (("escape", {PREFIX + "../../evil": b"x"}),
                              ("link", None)):
            debs = dict(self.debs)
            if members is not None:
                debs["libobasis9.9-core_9.9.0-1_amd64.deb"] = build_deb(
                    {**members, PREFIX + "program/soffice": b"#!/bin/sh\n"})
            else:
                data = io.BytesIO()
                with tarfile.open(fileobj=data, mode="w:xz") as archive:
                    info = tarfile.TarInfo(PREFIX + "program/soffice")
                    info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
                    archive.addfile(info)
                    info = tarfile.TarInfo(PREFIX + "program/dev")
                    info.type = tarfile.CHRTYPE
                    archive.addfile(info)
                debs["libobasis9.9-core_9.9.0-1_amd64.deb"] = self.manual_deb(data.getvalue())
            bundle = self.base / (name + ".tgz")
            build_bundle(bundle, debs)
            raw = bundle.read_bytes()
            pins = {**self.pins, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            with self.subTest(case=name), self.assertRaises((provision.ProvisionError, tarfile.TarError)):
                provision.install_archive(bundle, pins, self.base / ("local-" + name))
            self.assertFalse((self.base / "evil").exists())
            self.assertFalse((self.base / ("local-" + name) / "9.9.0.1").exists())

    @staticmethod
    def manual_deb(body):
        out = b"!<arch>\n"
        for member, payload in (("debian-binary", b"2.0\n"), ("data.tar.xz", body)):
            out += member.ljust(16).encode() + b"0".ljust(12) + b"0".ljust(6) + b"0".ljust(6) + b"100644".ljust(8) \
                + str(len(payload)).ljust(10).encode() + b"`\n" + payload + (b"\n" if len(payload) % 2 else b"")
        return out

    def test_the_download_refuses_plain_http_and_oversize_bodies(self):
        with self.assertRaises(provision.ProvisionError):
            provision.download("http://example.invalid/x", self.base / "x", self.pins)

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False
        with unittest.mock.patch.object(provision.urllib.request, "urlopen",
                                        return_value=Response(b"x" * 2048)):
            with self.assertRaises(provision.ProvisionError):
                provision.download(self.pins["url"], self.base / "dl" / "x", {**self.pins, "bytes": 1024})
        self.assertEqual([p.name for p in (self.base / "dl").iterdir()], [])

    def test_the_real_pins_name_an_https_url_a_size_and_a_sha256(self):
        self.assertTrue(provision.PINS["url"].startswith("https://download.documentfoundation.org/"))
        self.assertRegex(provision.PINS["sha256"], r"^[0-9a-f]{64}$")
        self.assertGreater(provision.PINS["bytes"], 100 * 1024 * 1024)
        done = subprocess.run([sys.executable, "-m", "tools.extraction.provision_libreoffice",
                               "install", "--print-pins"], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(done.stdout)["sha256"], provision.PINS["sha256"])


class PreflightTests(unittest.TestCase):
    def test_the_preflight_check_is_informational(self):
        from types import SimpleNamespace
        from the_librarian_mcp import preflight
        missing = {runtime.RUNTIME_ENV: "/nonexistent/soffice"}  # the check runs in a child process
        with tempfile.TemporaryDirectory() as root, unittest.mock.patch.dict(os.environ, missing):
            config = SimpleNamespace(root=Path(root), toolkit_root=Path(__file__).resolve().parents[1],
                                     timeout_seconds=60)
            check = preflight.legacy_converter_runtime(config)
        self.assertTrue(check["ok"])
        self.assertIs(check["provisioned"], False)


REAL = runtime.locate(system=False)  # a system package on PATH never decides which tests run


@unittest.skipUnless(REAL is not None and runtime.sandbox_mode() is not None, "LibreOffice runtime not provisioned")
class RealRuntimeTests(unittest.TestCase):
    """Converts synthetic documents with the real runtime inside the sandbox (CI: libreoffice.yml)."""

    def test_verify_converts_a_synthetic_document_in_the_sandbox(self):
        report = provision.verify()
        self.assertTrue(report["verified"])
        self.assertTrue(report["runtime"]["soffice"].startswith("LibreOffice"))

    def test_a_legacy_document_converts_to_markdown_through_the_sandbox(self):
        sample = provision.SAMPLE.encode()
        with tempfile.TemporaryDirectory() as parent:
            with runtime.exported(parent, sample, ".fodt", "doc") as produced:
                legacy = produced.read_bytes()
            self.assertEqual(legacy[:8], convert.OLE)
            with runtime.exported(parent, legacy, ".doc", "docx") as produced:
                markdown, fidelity = convert._worker("docx", produced, hashlib.sha256(produced.read_bytes()).hexdigest())
            self.assertEqual(list(Path(parent).iterdir()), [])
        self.assertIn("Librarian converter verification paragraph", markdown)

    def test_a_corrupt_legacy_file_ends_with_a_result_or_a_code_and_leaves_nothing_behind(self):
        with tempfile.TemporaryDirectory() as parent:
            try:
                with runtime.exported(parent, convert.OLE + os.urandom(2048), ".doc", "docx", 120):
                    pass
            except bounded.BoundedError as error:
                self.assertIn(error.code, ("legacy-conversion-failed", "legacy-conversion-crashed"))
            self.assertEqual(list(Path(parent).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
