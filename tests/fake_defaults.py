"""In-memory stand-in for /usr/bin/defaults, killall and sudo."""

import plistlib

from tools.user.defaults import DefaultsCLI

DEFAULTS = "/usr/bin/defaults"
KILLALL = "/usr/bin/killall"
SUDO = "/usr/bin/sudo"


class FakeDefaults:
    def __init__(self):
        self.store = {}  # (scope, domain) -> {key: value}
        self.calls = []
        self.fail = {}  # verb -> stderr, makes that verb exit 1
        self.sudo_ok = True
        self.running = {"Dock", "Finder"}

    def domain(self, domain, scope="user"):
        return self.store.setdefault((scope, domain), {})

    def run(self, cmd, env=None, cwd=None):
        self.calls.append(list(cmd))
        args = list(cmd)
        privileged = False
        if args[0] == SUDO:
            if args[1] != "-n":
                raise AssertionError(f"sudo without -n: {cmd}")
            if not self.sudo_ok:
                return 1, "", "sudo: a password is required"
            privileged = True
            args = args[2:]
        if args[0] == KILLALL:
            if args[1] in self.running:
                return 0, "", ""
            return 1, "", "No matching processes belonging to you were found"
        assert args[0] == DEFAULTS, cmd
        args = args[1:]
        scope = "user"
        if args[0] == "-currentHost":
            scope, args = "currentHost", args[1:]
        if args[1].startswith("/Library/"):
            scope = "system"
        verb = args[0]
        if verb in self.fail:
            return 1, "", self.fail[verb]
        if scope == "system" and verb in ("write", "delete") and not privileged:
            return 1, "", "Could not write domain; exiting"
        values = self.domain(args[1], scope)
        if verb == "export":
            assert args[2] == "-"
            return 0, plistlib.dumps(values, fmt=plistlib.FMT_XML).decode(), ""
        if verb == "write":
            values[args[2]] = plistlib.loads(args[3].encode())
            return 0, "", ""
        if verb == "delete":
            if args[2] not in values:
                return 1, "", "Domain pair not found"
            del values[args[2]]
            return 0, "", ""
        raise AssertionError(f"unexpected defaults call {cmd}")

    def raise_on(self, verb, lands, exc=OSError("timed out")):
        """Make `verb` raise exc, after applying it when lands. Returns an undo."""
        orig = self.run

        def run(cmd, env=None, cwd=None):
            if verb in cmd:
                if lands:
                    orig(cmd, env, cwd)
                raise exc
            return orig(cmd, env, cwd)

        self.run = run
        return lambda: setattr(self, "run", orig)

    def cli(self, allow_sudo=True, **kwargs):
        return DefaultsCLI(
            defaults=DEFAULTS,
            killall=KILLALL,
            sudo=SUDO,
            run=self.run,
            allow_sudo=allow_sudo,
            **kwargs,
        )

    def verbs(self):
        out = []
        for cmd in self.calls:
            args = cmd[2:] if cmd[0] == SUDO else cmd
            if args[0] == KILLALL:
                out.append(("killall", args[1]))
            else:
                args = [a for a in args[1:] if a != "-currentHost"]
                out.append((args[0], *args[1:3]))
        return out

    def writes(self):
        return [v for v in self.verbs() if v[0] in ("write", "delete", "killall")]
