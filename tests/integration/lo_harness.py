"""Starts a headless LibreOffice with a throwaway profile for integration tests."""

import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time

import uno
import unohelper
from com.sun.star.beans import PropertyValue

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OXT = os.path.join(ROOT, "dist", "ciu-libreoffice.oxt")
MAC_BIN = "/Applications/LibreOffice.app/Contents/MacOS"


def _tool(name):
    """soffice / unopkg: $SOFFICE_DIR, then PATH, then the macOS app bundle."""
    d = os.environ.get("SOFFICE_DIR")
    if d:
        return os.path.join(d, name)
    return shutil.which(name) or os.path.join(MAC_BIN, name)


# unopkg refuses per-user installs as root.
_SCOPE = ["--shared"] if hasattr(os, "geteuid") and os.geteuid() == 0 else []


def prop(name, value):
    p = PropertyValue()
    p.Name, p.Value = name, value
    return p


class Office(object):
    def __init__(self, port=2099, profile=None):
        self.port = port
        self.profile = profile or tempfile.mkdtemp(prefix="ciu_lo_profile_")
        self.proc = None
        self.ctx = None

    def start(self, env=None, scripts=None):
        """env: extra environment for soffice. scripts: Python macro files to
        copy into the profile's user Scripts/python folder.
        """
        if scripts:
            dest = os.path.join(self.profile, "user", "Scripts", "python")
            os.makedirs(dest, exist_ok=True)
            for s in scripts:
                shutil.copy(s, dest)
        if _port_open(self.port):
            raise RuntimeError("port %d already in use (stray soffice?)" % self.port)
        full_env = dict(os.environ)
        full_env.update(env or {})
        self.proc = subprocess.Popen([
            _tool("soffice"), "--headless", "--invisible", "--nologo", "--norestore",
            "-env:UserInstallation=" + uno.systemPathToFileUrl(self.profile),
            "--accept=socket,host=127.0.0.1,port=%d;urp;" % self.port,
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=full_env,
            start_new_session=True)
        local = uno.getComponentContext()
        resolver = local.ServiceManager.createInstanceWithContext(
            "com.sun.star.bridge.UnoUrlResolver", local)
        url = "uno:socket,host=127.0.0.1,port=%d;urp;StarOffice.ComponentContext" % self.port
        for _ in range(120):
            try:
                self.ctx = resolver.resolve(url)
                return self
            except Exception:
                time.sleep(0.5)
        raise RuntimeError("LibreOffice did not start")

    @property
    def smgr(self):
        return self.ctx.ServiceManager

    def desktop(self):
        return self.smgr.createInstanceWithContext("com.sun.star.frame.Desktop", self.ctx)

    @staticmethod
    def install_shared(oxt=OXT):
        """Install with unopkg before LibreOffice starts (shared as root,
        per-user otherwise). --force replaces an existing copy.
        """
        subprocess.run([_tool("unopkg"), "add"] + _SCOPE + ["--force", oxt], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @staticmethod
    def remove_shared(ext_id="com.runciu.ciu.libreoffice"):
        subprocess.run([_tool("unopkg"), "remove"] + _SCOPE + [ext_id],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def installed(self):
        mgr = self.ctx.getValueByName("/singletons/com.sun.star.deployment.ExtensionManager")
        return [p.getIdentifier().Value
                for scope in ("user", "shared")
                for p in mgr.getDeployedExtensions(scope, None, None)]

    def hook(self, command):
        """Send a command to the in-process test component (test build only)."""
        if not hasattr(self, "_hook"):
            self._hook = self.smgr.createInstanceWithContext("com.runciu.ciu.SelfTest", self.ctx)
            if self._hook is None:
                raise RuntimeError("test hook missing: install dist/ciu-libreoffice-test.oxt")
        result = self._hook.getByName(command)
        if isinstance(result, str) and result.startswith("ERROR\n"):
            raise AssertionError(result)
        return result

    def config(self, path):
        cp = self.smgr.createInstanceWithContext(
            "com.sun.star.configuration.ConfigurationProvider", self.ctx)
        return cp.createInstanceWithArguments(
            "com.sun.star.configuration.ConfigurationAccess", (prop("nodepath", path),))

    def new_calc(self):
        return self.desktop().loadComponentFromURL(
            "private:factory/scalc", "_blank", 0, (prop("Hidden", False),))

    def stop(self):
        try:
            self.desktop().terminate()
        except Exception:
            pass
        if self.proc:
            try:
                self.proc.wait(timeout=20)
            except Exception:
                pass
            # soffice.bin is a child of the launcher; kill the whole group.
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            for _ in range(40):
                if not _port_open(self.port):
                    break
                time.sleep(0.25)
        shutil.rmtree(self.profile, ignore_errors=True)


def _port_open(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0
