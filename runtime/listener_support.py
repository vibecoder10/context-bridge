"""One consumer per local project identity, and owner-controlled macOS launchd."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time

from relay import BridgeError

LABEL = "ai.contextbridge.listener"


class ListenerLock:
    def __init__(self, config_path):
        self.config = Path(config_path).resolve()
        value = json.loads(self.config.read_text())
        key = hashlib.sha256((value["project_id"] + "\0" + value["identity"]).encode()).hexdigest()[:24]
        self.path = self.config.parent / ("listener-" + key + ".lock")
        self.fd = None

    def acquire(self):
        fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise BridgeError("A listener already runs for this project. Stop it before starting another.")
        self.fd = fd
        return self

    def close(self):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None

    def held(self):
        probe = ListenerLock(self.config)
        try:
            probe.acquire()
        except BridgeError:
            return True
        finally:
            probe.close()
        return False


class BackgroundListener:
    def __init__(self, config_path, engine, home=None, runner=None):
        self.config = Path(config_path).resolve()
        self.engine = engine
        self.home = Path(home) if home else Path.home()
        self.runner = runner or subprocess.run
        self.label = LABEL
        if self.config.exists():
            value = json.loads(self.config.read_text())
            if value.get('consumer'):
                key = hashlib.sha256((value['project_id']+'\0'+value['identity']).encode()).hexdigest()[:16]
                self.label += '.' + key
        self.plist = self.home / "Library/LaunchAgents" / (self.label + ".plist")
        self.domain = "gui/" + str(os.getuid())

    def command(self, *args):
        return self.runner(["/bin/launchctl", *args], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=20)

    def definition(self):
        log = self.home / "Library/Logs/context-bridge" / (('listener' if self.label == LABEL else self.label) + '.log')
        return {"Label": self.label, "ProgramArguments": [str(Path(sys.executable).resolve()), "-u",
                str(Path(__file__).resolve().parent / "background_listener.py"),
                "--config", str(self.config), "--engine", self.engine],
                "WorkingDirectory": str(self.config.parent), "RunAtLoad": True, "KeepAlive": True,
                "StandardOutPath": str(log), "StandardErrorPath": str(log),
                "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin:" + str(self.home / ".local/bin")}}

    def owns_plist(self):
        if not self.plist.exists():
            return False
        data = plistlib.loads(self.plist.read_bytes())
        args = data.get("ProgramArguments", [])
        return "--config" in args and args[args.index("--config") + 1] == str(self.config)

    def status(self):
        return "on" if self.owns_plist() and self.command("print", self.domain + "/" + self.label).returncode == 0 else "off"

    def set(self, on):
        if type(on) is not bool:
            raise BridgeError("on must be true or false.")
        if self.plist.exists() and not self.owns_plist():
            raise BridgeError("Background listening is configured for another project. Turn it off there first.")
        if not on:
            if self.owns_plist():
                result = self.command("bootout", self.domain + "/" + self.label)
                if result.returncode and self.status() == "on":
                    raise BridgeError("The background listener could not be stopped.")
                # bootout can return before launchd has removed the job. Wait for
                # that removal before allowing an immediate restart after a grant change.
                deadline=time.monotonic()+4
                while self.command('print',self.domain+'/'+self.label).returncode==0:
                    if time.monotonic()>=deadline:raise BridgeError('Background listener is still stopping; retry after it exits.')
                    time.sleep(.1)
                self.plist.unlink(missing_ok=True)
            return {"background": "off"}
        if self.status() == "on":
            return {"background": "on"}
        if ListenerLock(self.config).held():
            raise BridgeError("Stop the current project listener before enabling background listening.")
        self.plist.parent.mkdir(parents=True, exist_ok=True)
        log = self.home / "Library/Logs/context-bridge" / (('listener' if self.label == LABEL else self.label) + '.log')
        log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(log), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        fd = os.open(str(self.plist), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            plistlib.dump(self.definition(), stream)
        result = self.command("bootstrap", self.domain, str(self.plist))
        if result.returncode or self.status() != "on":
            self.plist.unlink(missing_ok=True)
            raise BridgeError("Background listening could not start (launchctl code %s). Check whether the previous job has exited or macOS blocked the login item." % result.returncode)
        return {"background": "on"}
