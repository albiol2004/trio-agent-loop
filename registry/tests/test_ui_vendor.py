"""Vendored Preact + signals + htm (GOAL DoD6 vendoring): the files serve.py
serves under /vendor/, the checksum guard, /ui/ module serving and
dashboard/vendor/vendor.py --check.

The servers are real DashboardServer instances on ephemeral loopback ports;
requests are sent with http.client so the request path is exactly the one
written (no client-side normalisation of ``..``). A tampered file only ever
exists in a COPY of the dashboard tree that serve.DASHBOARD_DIR is pointed
at; the real vendored files are never modified.
"""
from __future__ import annotations

import hashlib
import http.client
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DASHBOARD = REPO_ROOT / "dashboard"
VENDOR = DASHBOARD / "vendor"
SERVE_PATH = DASHBOARD / "serve.py"
VENDOR_PY = VENDOR / "vendor.py"

# sha256 of the files inside the npm tarballs (package/dist/preact.module.js of
# preact-10.29.8.tgz, ...), computed from the tarballs whose dist.integrity
# matched the registry's when the files were vendored.
TARBALL_SHA256 = {
    "preact/preact.module.js": "c30e721ebfdc6e2ad4c18c14d2dfb82667829c8aec27de1207774e3fc16858a8",
    "preact/hooks.module.js": "a6ee626f2d01570592dd569a792e3f050154aa02890eead8c223fa3ed5aa3d5a",
    "signals-core/signals-core.mjs": "0676238240715faeb4eb8b0a329837dc06ef7f37e7103601e13af3ed309c042c",
    "signals/signals.module.js": "baa602be76dbecd6f4b3d1a4ff5b5ec694bc22a93f7a1a801bdbdca636d211ed",
    "htm/htm.module.js": "ab33dd3f38059b9be4d5f5350128eefb2356639c4e0bbe9d9e8b3ba75847e9e4",
}
VERSIONS = {
    "preact": "10.29.8", "@preact/signals-core": "1.14.4",
    "@preact/signals": "2.11.3", "htm": "3.1.1",
}
UI_MODULES = ("store", "format", "transport", "common")


def _load_serve_module():
    spec = importlib.util.spec_from_file_location("trio_dashboard_serve_vendor", SERVE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


serve = _load_serve_module()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def manifest() -> dict:
    return json.loads((VENDOR / "MANIFEST.json").read_text(encoding="utf-8"))


def importmap() -> dict:
    return json.loads((VENDOR / "importmap.json").read_text(encoding="utf-8"))["imports"]


class LabServer(unittest.TestCase):
    """A DashboardServer on an ephemeral loopback port, static routes only."""

    dashboard_dir: Path | None = None  # copy of the tree to serve, or the real one

    def setUp(self):
        patcher = patch.object(serve, "DASHBOARD_DIR", self.dashboard_dir or DASHBOARD)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = serve.DashboardServer(
            ("127.0.0.1", 0), workspaces=[REPO_ROOT], auto_discover=False)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def get(self, path: str, headers: dict | None = None):
        """(status, headers, body) of GET `path`, sent verbatim."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest("GET", path, skip_host=bool(headers and "Host" in headers))
            for name, value in (headers or {}).items():
                conn.putheader(name, value)
            conn.endheaders()
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
        finally:
            conn.close()


class VendorRouteTests(LabServer):
    def test_every_import_map_target_is_served_with_its_manifest_sha256(self):
        # accept: GET /vendor/<importmap target> -> 200 text/javascript, body sha256 ==
        # MANIFEST.json entry == sha256 of the file inside the npm tarball
        files = {rel: entry["sha256"] for pkg in manifest()["packages"].values()
                 for rel, entry in pkg["files"].items()}
        targets = importmap()
        self.assertEqual(set(targets), {
            "preact", "preact/hooks", "@preact/signals-core", "@preact/signals", "htm"})
        for spec, url in targets.items():
            with self.subTest(spec=spec):
                self.assertTrue(url.startswith("/vendor/"))
                status, headers, body = self.get(url)
                self.assertEqual(status, 200)
                self.assertEqual(headers["content-type"], "text/javascript; charset=utf-8")
                self.assertEqual(headers["cache-control"], "no-cache")
                rel = url[len("/vendor/"):]
                self.assertEqual(sha256(body), files[rel])
                self.assertEqual(sha256(body), TARBALL_SHA256[rel])
                self.assertEqual(body, (VENDOR / rel).read_bytes())

    def test_licenses_are_served_as_plain_text(self):
        for pkg in manifest()["packages"].values():
            for rel in pkg["files"]:
                if not rel.endswith("LICENSE"):
                    continue
                with self.subTest(rel=rel):
                    status, headers, body = self.get("/vendor/" + rel)
                    self.assertEqual(status, 200)
                    self.assertEqual(headers["content-type"], "text/plain; charset=utf-8")
                    self.assertEqual(body, (VENDOR / rel).read_bytes())

    def test_unlisted_and_traversal_paths_are_404(self):
        # accept: /vendor/not-listed.js, /vendor/../serve.py, /vendor/%2e%2e/serve.py -> 404
        for path in (
            "/vendor/not-listed.js",
            "/vendor/../serve.py",
            "/vendor/%2e%2e/serve.py",
            "/vendor/%2E%2E/serve.py",
            "/vendor/preact/../../serve.py",
            "/vendor/preact/%2e%2e/%2e%2e/serve.py",
            "/vendor/preact%2Fpreact.module.js",
            "/vendor/..%2fserve.py",
            "/vendor//etc/passwd",
            "/vendor/etc/passwd",
            "/vendor/",
            "/vendor/preact/",
            "/vendor/preact",
            "/vendor/MANIFEST.json",
            "/vendor/importmap.json",
            "/vendor/vendor.py",
            "/vendor/preact/preact.module.js.map",
            "/vendor/preact/preact.module.js/",
            "/vendor/PREACT/preact.module.js",
        ):
            with self.subTest(path=path):
                status, headers, body = self.get(path)
                self.assertEqual(status, 404)
                self.assertNotIn("javascript", headers.get("content-type", ""))
                self.assertNotIn(b"function", body)

    def test_ui_modules_are_served_by_name(self):
        for name in UI_MODULES:
            with self.subTest(name=name):
                status, headers, body = self.get(f"/ui/{name}.js")
                self.assertEqual(status, 200)
                self.assertEqual(headers["content-type"], "text/javascript; charset=utf-8")
                self.assertEqual(headers["cache-control"], "no-cache")
                self.assertEqual(body, (DASHBOARD / "ui" / f"{name}.js").read_bytes())

    def test_bad_ui_names_are_404(self):
        # accept: /ui/../app.js, /ui/Bad.js -> 404
        for path in (
            "/ui/../app.js", "/ui/Bad.js", "/ui/%2e%2e/app.js", "/ui/..%2fapp.js",
            "/ui/store.js/", "/ui/store", "/ui/", "/ui/.store.js", "/ui/_store.js",
            "/ui/1store.js", "/ui/sto%20re.js", "/ui/store.JS", "/ui/store.js.map",
            "/ui/nested/store.js", "/ui/does-not-exist.js", "/ui/Store.js",
        ):
            with self.subTest(path=path):
                status, headers, body = self.get(path)
                self.assertEqual(status, 404)
                self.assertNotIn("javascript", headers.get("content-type", ""))

    def test_host_header_is_checked_like_every_static_route(self):
        for path in ("/vendor/preact/preact.module.js", "/ui/store.js"):
            with self.subTest(path=path):
                status, _headers, body = self.get(path, {"Host": "evil.example"})
                self.assertEqual(status, 421)
                self.assertNotIn(b"function", body)

    def test_existing_static_routes_still_serve(self):
        for path in ("/", "/app.js", "/app.css"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 200)


class TamperedVendorTests(LabServer):
    """A vendored file altered on disk (in a copy of the tree) is refused."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="ui-vendor-"))
        cls.tree = cls.tmp / "dashboard"
        (cls.tree).mkdir()
        shutil.copytree(VENDOR, cls.tree / "vendor")
        shutil.copytree(DASHBOARD / "ui", cls.tree / "ui")
        cls.dashboard_dir = cls.tree
        target = cls.tree / "vendor" / "preact" / "preact.module.js"
        data = bytearray(target.read_bytes())
        data[100] ^= 0x01
        target.write_bytes(bytes(data))
        cls.real_before = {rel: sha256((VENDOR / rel).read_bytes())
                           for pkg in manifest()["packages"].values() for rel in pkg["files"]}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        cls.dashboard_dir = None

    def test_tampered_file_is_500_with_no_js_body(self):
        # accept: a vendored file altered on disk -> 500 {error: 'vendor checksum mismatch'}
        status, headers, body = self.get("/vendor/preact/preact.module.js")
        self.assertEqual(status, 500)
        self.assertIn("application/json", headers["content-type"])
        self.assertEqual(json.loads(body), {"error": "vendor checksum mismatch"})
        self.assertNotIn(b"function", body)
        self.assertLess(len(body), 100)

    def test_untouched_files_of_the_same_tree_still_serve(self):
        status, _headers, body = self.get("/vendor/htm/htm.module.js")
        self.assertEqual(status, 200)
        self.assertEqual(sha256(body), TARBALL_SHA256["htm/htm.module.js"])

    def test_a_deleted_listed_file_is_not_served(self):
        victim = self.tree / "vendor" / "signals" / "signals.module.js"
        saved = victim.read_bytes()
        victim.unlink()
        try:
            status, _headers, body = self.get("/vendor/signals/signals.module.js")
            self.assertEqual(status, 404)
            self.assertNotIn(b"function", body)
        finally:
            victim.write_bytes(saved)

    def test_the_real_vendored_files_were_not_touched(self):
        for rel, digest in self.real_before.items():
            self.assertEqual(sha256((VENDOR / rel).read_bytes()), digest, rel)
        self.assertEqual(sha256((VENDOR / "preact/preact.module.js").read_bytes()),
                         TARBALL_SHA256["preact/preact.module.js"])


def run_check(vendor_py: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(vendor_py), "--check"],
                          capture_output=True, text=True, timeout=60)


class VendorCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ui-vendor-check-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.copy = self.tmp / "vendor"
        shutil.copytree(VENDOR, self.copy)

    def test_check_passes_on_the_repository(self):
        # accept: vendor.py --check -> exit 0
        proc = run_check(VENDOR_PY)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("9 files verified", proc.stdout)

    def test_one_flipped_byte_fails_and_names_the_file(self):
        # accept: one byte flipped -> exit 1 naming the file
        target = self.copy / "htm" / "htm.module.js"
        data = bytearray(target.read_bytes())
        data[10] ^= 0x01
        target.write_bytes(bytes(data))
        proc = run_check(self.copy / "vendor.py")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("htm/htm.module.js", proc.stderr)
        self.assertNotIn("preact", proc.stderr)

    def test_a_missing_file_fails_and_names_it(self):
        (self.copy / "signals" / "LICENSE").unlink()
        proc = run_check(self.copy / "vendor.py")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("signals/LICENSE", proc.stderr)

    def test_an_import_map_target_outside_the_manifest_fails(self):
        path = self.copy / "importmap.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["imports"]["htm"] = "/vendor/htm/not-in-manifest.js"
        path.write_text(json.dumps(data), encoding="utf-8")
        proc = run_check(self.copy / "vendor.py")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("htm", proc.stderr)
        self.assertIn("not-in-manifest.js", proc.stderr)

    def test_an_import_map_target_must_be_a_vendor_url(self):
        path = self.copy / "importmap.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["imports"]["htm"] = "https://cdn.example/htm.js"
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(run_check(self.copy / "vendor.py").returncode, 1)


class VendorContentTests(unittest.TestCase):
    def test_manifest_pins_exact_versions_and_integrity(self):
        packages = manifest()["packages"]
        self.assertEqual(manifest()["schema"], 1)
        self.assertEqual({n: p["version"] for n, p in packages.items()}, VERSIONS)
        for name, pkg in packages.items():
            with self.subTest(package=name):
                self.assertRegex(pkg["version"], r"^\d+\.\d+\.\d+$")
                self.assertRegex(pkg["integrity"], r"^sha512-[A-Za-z0-9+/]{86}==$")
                self.assertRegex(pkg["tarball"],
                                 r"^https://registry\.npmjs\.org/.+-" + re.escape(pkg["version"]) + r"\.tgz$")
                self.assertTrue(any(rel.endswith("LICENSE") for rel in pkg["files"]))
                for rel, entry in pkg["files"].items():
                    self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")
                    self.assertTrue(entry["from"].startswith("package/"))

    def test_every_file_on_disk_is_listed_in_the_manifest(self):
        listed = {rel for pkg in manifest()["packages"].values() for rel in pkg["files"]}
        on_disk = {str(p.relative_to(VENDOR)) for p in VENDOR.rglob("*")
                   if p.is_file() and "__pycache__" not in p.parts}
        self.assertEqual(on_disk - listed,
                         {"MANIFEST.json", "importmap.json", "vendor.py"})
        self.assertEqual(listed - on_disk, set())

    def test_import_map_has_no_remote_url(self):
        text = (VENDOR / "importmap.json").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"https?://")
        for url in importmap().values():
            self.assertTrue(url.startswith("/vendor/"), url)

    def test_every_bare_import_of_the_vendored_and_ui_modules_resolves(self):
        known = set(importmap())
        pattern = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(?\s*)(["'])([^"']+)\1""")
        sources = [VENDOR / rel for pkg in manifest()["packages"].values()
                   for rel in pkg["files"] if rel.endswith((".js", ".mjs"))]
        sources += sorted((DASHBOARD / "ui").glob("*.js"))
        self.assertGreaterEqual(len(sources), 5 + len(UI_MODULES))
        bare_seen = set()
        for path in sources:
            for _quote, spec in pattern.findall(path.read_text(encoding="utf-8")):
                with self.subTest(file=path.name, spec=spec):
                    self.assertNotRegex(spec, r"^(?:https?:)?//", "remote import")
                    if spec.startswith((".", "/")):
                        continue
                    bare_seen.add(spec)
                    self.assertIn(spec, known)
        # the vendored modules really do import through the map
        self.assertTrue({"preact", "preact/hooks", "@preact/signals-core"} <= bare_seen)


if __name__ == "__main__":
    unittest.main()
