import hashlib
import os
import plistlib
import pwd
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from click.testing import CliRunner

from tools import cli
from tools.user import launchd

UID = 501


class FakeSystem:
    """Stand-in for launchctl: tracks loaded and disabled jobs, acts out file ops."""

    def __init__(self):
        self.calls = []
        self.loaded = {}  # label -> plist path
        self.disabled = set()
        self.script = {}  # (verb, label) -> list of rc to return before default behaviour
        self.linger = {}  # label -> bootout answers EINPROGRESS
        self.system_calls = []  # file ops, and launchctl changes in the system domain
        self.fail_tools = set()
        self.disabled_rc = 119  # what bootstrap of a launchd-disabled job returns
        self.print_disabled_rc = 0
        self.root_dirs = set()  # dirs only root can see (parent not enterable by the user)

    def _next(self, verb, label):
        queue = self.script.get((verb, label))
        return queue.pop(0) if queue else None

    def run(self, cmd):
        self.calls.append(list(cmd))
        assert not any("sudo" in c for c in cmd), f"tools must never call sudo: {cmd}"
        tool = cmd[0]
        if tool != launchd.LAUNCHCTL or (
            cmd[1] not in ("print", "print-disabled") and any(c.startswith("system") for c in cmd)
        ):
            self.system_calls.append(list(cmd))
        if tool != launchd.LAUNCHCTL:
            # File ops get flags then absolute paths only; anything else would touch the cwd.
            paths = [c for c in cmd[1:] if not c.startswith("-") and "/" in c]
            assert paths and all(os.path.isabs(c) for c in paths), f"bad argv {cmd}"
            assert all(c.startswith("-") or os.path.isabs(c) or c.isalnum() or ":" in c
                       or c in ("xml1", "256") for c in cmd[1:]), f"relative arg {cmd}"  # fmt: skip
        if tool in self.fail_tools:
            return 1, "", f"{tool} failed"
        if tool == launchd.LAUNCHCTL:
            return self._launchctl(cmd[1:])
        if tool == "/usr/bin/install":
            if "-d" in cmd:
                os.makedirs(cmd[-1], exist_ok=True)
            elif cmd[-2] == "/dev/null":
                open(cmd[-1], "x").close()
            else:
                # Real /Library/LaunchDaemons always exists; the tmp stand-in may not.
                os.makedirs(os.path.dirname(cmd[-1]), exist_ok=True)
                shutil.copyfile(cmd[-2], cmd[-1])
            if "-m" in cmd:
                os.chmod(cmd[-1], int(cmd[cmd.index("-m") + 1], 8))
        elif tool == "/bin/rm":
            os.remove(cmd[-1])
        elif tool == "/bin/mkdir":
            os.makedirs(cmd[-1], exist_ok=True)
        elif tool == "/usr/bin/touch":
            for f in cmd[1:]:
                if not f.startswith("-"):
                    open(f, "a").close()
        elif tool == "/usr/sbin/chown":
            pass
        elif tool == "/bin/test":
            assert cmd[1] == "-d", cmd
            return (0 if cmd[-1] in self.root_dirs or os.path.isdir(cmd[-1]) else 1), "", ""
        elif tool == "/usr/bin/shasum":
            fd = os.open(cmd[-1], os.O_RDONLY)
            try:
                data = os.read(fd, 1 << 20)
            finally:
                os.close(fd)
            return 0, f"{hashlib.sha256(data).hexdigest()}  {cmd[-1]}\n", ""
        elif tool == "/usr/bin/plutil":
            mode = os.stat(cmd[-1]).st_mode & 0o777
            os.chmod(cmd[-1], 0o600)
            try:
                with open(cmd[-1], "rb") as f:
                    out = plistlib.dumps(plistlib.load(f)).decode()
            finally:
                os.chmod(cmd[-1], mode)
            return 0, out, ""
        else:
            raise AssertionError(f"unexpected command {cmd}")
        return 0, "", ""

    def _launchctl(self, args):
        verb = args[0]
        label = args[-1].rsplit("/", 1)[-1]
        if verb == "bootstrap":
            label = os.path.basename(args[-1])[: -len(".plist")]
        if verb == "print-disabled":
            if self.print_disabled_rc:
                return self.print_disabled_rc, "", "Operation not permitted"
            body = "".join(f'\t"{lbl}" => disabled\n' for lbl in sorted(self.disabled))
            return 0, f"disabled services = {{\n{body}}}\n", ""
        forced = self._next(verb, label)
        if forced is not None:
            return forced, "", "forced failure"
        target = args[1] if verb == "bootstrap" else args[-1]
        here = label in self.loaded and (
            ("LaunchDaemons" in self.loaded[label]) == target.startswith("system")
        )
        if verb == "print":
            if here:
                return (
                    0,
                    f"{label} = {{\n\tpath = {self.loaded[label]}\n\tstate = running\n"
                    "\tpid = 42\n\tlast exit code = 0\n}\n",
                    "",
                )
            return 113, "", "Could not find service"
        if verb == "bootout":
            if not here:
                return 3, "", "No such process"
            del self.loaded[label]
            if self.linger.pop(label, False):
                return 36, "", "Operation now in progress"
            return 0, "", ""
        if verb == "bootstrap":
            if label in self.disabled:
                return self.disabled_rc, "", "Service is disabled"
            if here:
                return 5, "", "Input/output error"
            self.loaded[label] = args[-1]
            return 0, "", ""
        if verb == "enable":
            self.disabled.discard(label)
            return 0, "", ""
        if verb == "disable":
            self.disabled.add(label)
            return 0, "", ""
        if verb == "kickstart":
            return (0, "", "") if here else (113, "", "not found")
        raise AssertionError(f"unexpected launchctl {args}")

    def verbs(self, verb):
        return [c for c in self.calls if launchd.LAUNCHCTL in c and verb in c]


class LaunchdTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.fake = FakeSystem()
        self.env = launchd.Env(
            agents_dir=os.path.join(self.tmp, "LaunchAgents"),
            daemons_dir=os.path.join(self.tmp, "LaunchDaemons"),
            user_log_dir=os.path.join(self.tmp, "logs"),
            system_log_dir=os.path.join(self.tmp, "syslogs"),
            run=self.fake.run,
            sleep=lambda _s: None,
            system_log_roots=(),
            bash="/bin/bash",
            uid=UID,
            euid=UID,
            home=self.tmp,
            is_darwin=True,
            volumes_root=os.path.join(self.tmp, "Volumes"),
            ismount=lambda _p: True,
            lstat=self.owner_view(os.lstat),
            stat=self.owner_view(os.stat),
        )
        self.state = {}

    def tearDown(self):
        self._tmp.cleanup()

    def owner_view(self, real):
        """Stat as root sees it on a real host: everything root-only except paths in
        `user_owned` (owned by UID) and `writable` (group/world-writable)."""
        self.user_owned, self.writable = set(), set()

        def view(path):
            st = real(path)
            mode = st.st_mode | 0o022 if path in self.writable else st.st_mode & ~0o022
            return SimpleNamespace(st_uid=UID if path in self.user_owned else 0, st_mode=mode)

        return view

    def cfg(self, agents=None, daemons=None, root=None, **section):
        """A launchd section; configuring daemons runs it under sudo unless root=False."""
        raw = {"labelPrefix": "org.example", **section}
        if agents is not None:
            raw["agents"] = agents
        if daemons is not None:
            raw["daemons"] = daemons
        if root or (root is None and daemons is not None):
            self.as_root()
        elif root is False:
            self.as_user()
        return raw

    def as_root(self):
        """`sudo tools deploy`: euid 0 acting for UID, whose home holds no daemon paths."""
        self.env.euid = 0
        self.env.home = os.path.join(self.tmp, "home")

    def as_user(self):
        self.env.euid = UID
        self.env.home = self.tmp

    def deploy(self, raw):
        return launchd.install_launchd(raw, self.state, self.env)

    def diff(self, raw):
        return launchd.diff_launchd(raw, self.state, self.env)

    def agent_path(self, name):
        return os.path.join(self.env.agents_dir, f"org.example.{name}.plist")

    def daemon_path(self, name):
        return os.path.join(self.env.daemons_dir, f"org.example.{name}.plist")

    def read(self, path):
        with open(path, "rb") as f:
            return plistlib.load(f)


class ParseTest(unittest.TestCase):
    def parse(self, agents=None, daemons=None, **section):
        raw = {"labelPrefix": "org.example", **section}
        raw.update({k: v for k, v in (("agents", agents), ("daemons", daemons)) if v is not None})
        return launchd.parse_launchd(raw)

    def test_defaults_and_label_prefix(self):
        _s, svcs, errors = self.parse({"web": {"command": "srv"}})
        self.assertEqual(errors, [])
        self.assertEqual(svcs[0].label, "org.example.web")
        self.assertEqual(svcs[0].domain, "user")

    def test_agents_and_daemons_set_the_domain(self):
        _s, svcs, errors = self.parse({"a": {"command": "x"}}, {"d": {"command": "y"}})
        self.assertEqual(errors, [])
        self.assertEqual([(s.name, s.domain) for s in svcs], [("a", "user"), ("d", "system")])

    def test_old_keys_name_their_replacement(self):
        cases = {
            "services": ({"services": {"x": {"command": "y"}}}, "launchd.agents"),
            "sudo": ({"sudo": True}, "sudo tools deploy --scope launchd"),
        }
        for key, (section, hint) in cases.items():
            with self.subTest(key):
                settings, svcs, errors = self.parse(**section)
                self.assertEqual(svcs, [])
                self.assertIn(f"`launchd.{key}`", errors[0])
                self.assertIn(hint, errors[0])
                self.assertEqual(settings.errors, errors)
        _s, svcs, errors = self.parse({"x": {"command": "y", "domain": "system"}})
        self.assertEqual(svcs, [])
        self.assertIn("agents.x: `domain` removed", errors[0])
        self.assertIn("launchd.daemons", errors[0])

    def test_same_label_in_agents_and_daemons_is_an_error(self):
        _s, svcs, errors = self.parse({"web": {"command": "x"}}, {"web": {"command": "y"}})
        self.assertEqual([s.domain for s in svcs], ["user"])
        self.assertEqual(errors, ["daemons.web: label org.example.web already used by agents.web"])

    def test_non_mapping_group_is_a_section_error(self):
        settings, svcs, errors = self.parse(daemons=["x"])
        self.assertEqual(settings.errors, ["daemons must be a mapping of name -> job"])

    def test_validation_errors(self):
        cases = {
            "neither": {},
            "both": {"command": "x", "programArguments": ["x"]},
            "unknown": {"command": "x", "bogus": 1},
            "user-on-agent": {"command": "x", "user": "nobody"},
            "rel-wait": {"command": "x", "waitForPaths": ["relative/path"]},
            "keepalive": {"command": "x", "keepAlive": {"Whenever": True}},
            "calendar": {"command": "x", "startCalendarInterval": {"second": 1}},
            "mode": {"command": "x", "extraDirs": [{"path": "/d", "mode": 755}]},
            "reserved": {"command": "x", "extraServiceConfig": {"Label": "y"}},
            "process": {"command": "x", "processType": "Fast"},
            "interval": {"command": "x", "startInterval": True},
        }
        for name, raw in cases.items():
            with self.subTest(name):
                _s, svcs, errors = self.parse({name: raw})
                self.assertEqual(svcs, [])
                self.assertTrue(errors and errors[0].startswith(f"agents.{name}:"), errors)

    def test_label_required_without_prefix(self):
        _s, svcs, errors = launchd.parse_launchd({"agents": {"x": {"command": "y"}}})
        self.assertEqual(svcs, [])
        self.assertIn("labelPrefix", errors[0])

    def test_duplicate_label_rejected(self):
        _s, svcs, errors = self.parse(
            {"a": {"command": "x", "label": "same"}, "b": {"command": "y", "label": "same"}}
        )
        self.assertEqual(len(svcs), 1)
        self.assertIn("already used", errors[0])

    def test_service_objects_can_be_built_without_parse(self):
        svc = launchd.Service(name="x", label="org.example.x", command="srv")
        pl = launchd.desired_plist(svc, launchd.Settings(), launchd.Env(is_darwin=True))
        self.assertEqual(pl["Label"], "org.example.x")


class PlistTest(LaunchdTestBase):
    def plist(self, raw, secrets_dir=None, daemon=False, **section):
        jobs = {"daemons" if daemon else "agents": {"svc": raw}}
        _s, svcs, errors = launchd.parse_launchd(self.cfg(**jobs, **section))
        self.assertEqual(errors, [])
        settings = launchd.parse_launchd(self.cfg({}, **section), secrets_dir=secrets_dir)[0]
        return launchd.desired_plist(svcs[0], settings, self.env)

    def test_command_is_inlined_through_bash_c(self):
        pl = self.plist({"command": "/opt/bin/srv --port 1"})
        self.assertEqual(pl["ProgramArguments"][:2], ["/bin/bash", "-c"])
        script = pl["ProgramArguments"][2]
        self.assertTrue(script.startswith("set -e\n"))
        self.assertTrue(script.rstrip().endswith("exec /opt/bin/srv --port 1"))
        self.assertEqual(len(pl["ProgramArguments"]), 3)
        self.assertEqual(
            (pl["RunAtLoad"], pl["KeepAlive"], pl["ThrottleInterval"]), (True, True, 10)
        )
        self.assertEqual(pl["StandardOutPath"], os.path.join(self.tmp, "logs", "svc.log"))
        self.assertEqual(pl["StandardErrorPath"], os.path.join(self.tmp, "logs", "svc.error.log"))

    def test_multiline_command_is_prefixed_like_nix(self):
        body = 'srv \\\n  --flag "$x"\n'
        script = self.plist({"command": body})["ProgramArguments"][2]
        self.assertTrue(script.endswith('exec srv \\\n  --flag "$x"\n'), script)

    def test_wrapper_order_wait_dirs_prestart_command(self):
        self.env.volumes_root = "/Volumes"
        script = self.plist(
            {
                "command": "srv",
                "waitForPaths": ["/Volumes/Ext Disk", "/run/secrets/token"],
                "dataDir": "/Volumes/Ext Disk/.svc",
                "extraDirs": [
                    "/tmp/a",
                    {"path": "/tmp/b", "mode": "0750", "owner": "me", "group": "staff"},
                ],
                "preStart": "render-config",
            },
            daemon=True,
        )["ProgramArguments"][2]
        order = [
            "Waiting for /Volumes/Ext Disk...",
            "while [ ! -e /run/secrets/token ]",
            "/bin/mkdir -p '/Volumes/Ext Disk/.svc'",
            "/bin/mkdir -p /tmp/a",
            "/bin/chmod -h 750 /tmp/b",
            "/usr/sbin/chown -h me:staff /tmp/b",
            "render-config",
            "exec srv",
        ]
        positions = [script.index(s) for s in order]
        self.assertEqual(positions, sorted(positions))

    def test_program_arguments_always_wrapped_like_nix(self):
        logs = os.path.join(self.tmp, "logs")
        pl = self.plist({"programArguments": ["/usr/bin/true", "a b"]})
        args = pl["ProgramArguments"]
        self.assertIn(f"/bin/mkdir -p {logs}", args[2])
        self.assertEqual(args[3:], ["org.example.svc", "/usr/bin/true", "a b"])

        pl = self.plist({"programArguments": ["/usr/bin/true", "a b"], "waitForPaths": "/x"})
        args = pl["ProgramArguments"]
        self.assertEqual(args[:2], ["/bin/bash", "-c"])
        self.assertTrue(args[2].rstrip().endswith('exec "$@"'))
        self.assertEqual(args[3:], ["org.example.svc", "/usr/bin/true", "a b"])

    def test_launchd_keys(self):
        pl = self.plist(
            {
                "command": "srv",
                "environment": {"PORT": 8080, "HOME": "/var/lib/svc"},
                "workingDirectory": "/var/lib/svc",
                "keepAlive": {"successfulExit": False, "pathState": {"/x": True}},
                "runAtLoad": False,
                "throttleInterval": 30,
                "processType": "Background",
                "nice": 5,
                "stdoutPath": "/tmp/o.log",
                "extraServiceConfig": {"SoftResourceLimits": {"NumberOfFiles": 1024}},
            }
        )
        self.assertEqual(pl["EnvironmentVariables"], {"PORT": "8080", "HOME": "/var/lib/svc"})
        self.assertEqual(pl["KeepAlive"], {"SuccessfulExit": False, "PathState": {"/x": True}})
        self.assertEqual(pl["WorkingDirectory"], "/var/lib/svc")
        self.assertEqual((pl["RunAtLoad"], pl["ThrottleInterval"]), (False, 30))
        self.assertEqual((pl["ProcessType"], pl["Nice"]), ("Background", 5))
        self.assertEqual(pl["StandardOutPath"], "/tmp/o.log")
        self.assertEqual(pl["SoftResourceLimits"], {"NumberOfFiles": 1024})

    def test_after_initial_demand_is_a_bool(self):
        pl = self.plist({"command": "x", "keepAlive": {"afterInitialDemand": True}})
        self.assertEqual(pl["KeepAlive"], {"AfterInitialDemand": True})
        _s, svcs, errors = launchd.parse_launchd(
            self.cfg({"s": {"command": "x", "keepAlive": {"afterInitialDemand": {"a": True}}}})
        )
        self.assertEqual(svcs, [])
        self.assertIn("afterInitialDemand", errors[0])

    def test_keep_alive_defaults_true_like_nix(self):
        pl = self.plist({"command": "x", "startInterval": 60})
        self.assertEqual((pl["StartInterval"], pl["KeepAlive"]), (60, True))
        pl = self.plist(
            {"command": "x", "startCalendarInterval": [{"hour": 22, "minute": 10}, {"weekday": 1}]}
        )
        self.assertEqual(pl["StartCalendarInterval"], [{"Hour": 22, "Minute": 10}, {"Weekday": 1}])
        self.assertTrue(pl["KeepAlive"])
        pl = self.plist({"command": "x", "startInterval": 60, "keepAlive": False})
        self.assertFalse(pl["KeepAlive"])

    def test_wrapper_matches_nix_starter(self):
        logs = os.path.join(self.tmp, "logs")
        script = self.plist(
            {"command": "srv", "waitForSecrets": True, "waitForPath": "/opt/ready",
             "waitForPaths": ["/opt/more"], "waitForMounts": ["/Volumes/Disk"],
             "dataDir": "/opt/data", "extraDirs": ["/opt/x"], "preStart": "prep"},
            secrets_dir=os.path.join(self.tmp, "secrets"),
        )["ProgramArguments"][2]  # fmt: skip
        self.assertEqual(
            script.splitlines(),
            [
                "set -e",
                'export PATH="/bin:/usr/bin:$PATH"',
                f"/bin/mkdir -p {logs}",
                f"if [ -O {logs} ] && [ ! -L {logs} ]; then /bin/chmod 755 {logs}; fi",
                'echo "Waiting for sops secrets..."',
                f"while [ ! -e {self.tmp}/secrets/.tools-rendered ]; do sleep 1; done",
                'echo "Sops secrets available."',
                "echo 'Waiting for mount /Volumes/Disk...'",
                'until [ -d /Volumes/Disk ] && [ "$(/usr/bin/stat -L -f %d /Volumes/Disk)" != '
                '"$(/usr/bin/stat -L -f %d /Volumes/Disk/..)" ]; do sleep 1; done',
                "echo '/Volumes/Disk mounted.'",
                "echo 'Waiting for /opt/ready...'",
                "while [ ! -e /opt/ready ]; do sleep 1; done",
                "echo '/opt/ready available.'",
                "echo 'Waiting for /opt/more...'",
                "while [ ! -e /opt/more ]; do sleep 1; done",
                "echo '/opt/more available.'",
                "/bin/mkdir -p /opt/data",
                "/bin/mkdir -p /opt/x",
                "prep",
                "exec srv",
            ],
        )
        _s, _svcs, errors = launchd.parse_launchd(
            self.cfg({"s": {"command": "x", "waitForPath": ["/a"]}})
        )
        self.assertIn("waitForPath must be a string", errors[0])

    def test_wait_for_mounts_parses_like_wait_for_paths(self):
        script = self.plist(
            {"command": "srv", "waitForMounts": "$VOL/Disk"}, daemon=True
        )["ProgramArguments"][2]  # fmt: skip
        self.assertIn(
            'until [ -d "${VOL}"/Disk ] && [ "$(/usr/bin/stat -L -f %d "${VOL}"/Disk)" != '
            '"$(/usr/bin/stat -L -f %d "${VOL}"/Disk/..)" ]; do sleep 1; done',
            script,
        )
        _s, _svcs, errors = launchd.parse_launchd(
            self.cfg({"s": {"command": "x", "waitForMounts": ["Volumes/Disk"]}})
        )
        self.assertIn("waitForMounts", errors[0])

    def test_daemon_waits_for_the_same_secrets_dir(self):
        script = self.plist(
            {"command": "srv", "waitForSecrets": True},
            secrets_dir="/state/secrets",
            daemon=True,
        )["ProgramArguments"][2]
        self.assertIn("while [ ! -e /state/secrets/.tools-rendered ]; do sleep 1; done", script)

    def test_wait_for_secrets_without_sops_secrets_dir_is_an_error(self):
        raw = self.cfg({"s": {"command": "srv", "waitForSecrets": True}})
        self.assertIn("  ! s: cannot build plist: waitForSecrets needs sops.secretsDir "
                      "(no sops section)", self.diff(raw))  # fmt: skip
        self.assertFalse(self.deploy(raw))
        why = "no sops secrets are configured"
        self.assertIn(
            f"  ! s: cannot build plist: waitForSecrets needs sops.secretsDir ({why})",
            launchd.diff_launchd(raw, self.state, self.env, secrets_dir_error=why),
        )
        self.assertTrue(
            launchd.install_launchd(raw, self.state, self.env, secrets_dir="/state/secrets")
        )
        with open(self.agent_path("s"), "rb") as f:
            script = plistlib.load(f)["ProgramArguments"][2]
        self.assertIn("while [ ! -e /state/secrets/.tools-rendered ]", script)

    def test_extra_service_config_user_owns_daemon_logs_like_nix(self):
        svc = {"command": "x", "extraServiceConfig": {"UserName": "_dns"}}
        raw = self.cfg(daemons={"dns": svc})
        self.assertTrue(launchd.install_launchd(raw, self.state, self.env))
        logs = self.env.system_log_dir
        self.assertIn(
            ["/usr/sbin/chown", "-h", "_dns:staff", os.path.join(logs, "dns.log"),
             os.path.join(logs, "dns.error.log")],
            self.fake.system_calls,
        )  # fmt: skip

    def test_daemon_user_group_and_log_dirs(self):
        pl = self.plist({"command": "x", "user": "svc", "group": "staff"}, daemon=True)
        self.assertEqual((pl["UserName"], pl["GroupName"]), ("svc", "staff"))
        syslogs = os.path.join(self.tmp, "syslogs")
        self.assertEqual(pl["StandardOutPath"], os.path.join(syslogs, "svc.log"))
        self.assertEqual(pl["StandardErrorPath"], os.path.join(syslogs, "svc.error.log"))
        env = launchd.Env(uid=UID, home=self.tmp)
        self.assertEqual(
            (env.system_log_dir, env.user_log_dir),
            ("/tmp/log/launchd", "/tmp/agents/log/launchd"),
        )
        self.assertNotIn("/bin/mkdir", pl["ProgramArguments"][2])
        script = self.plist({"command": "x"}, daemon=True)["ProgramArguments"][2].splitlines()
        self.assertEqual(
            script[2:4],
            [
                f"/bin/mkdir -p {syslogs}",
                f"if [ -O {syslogs} ] && [ ! -L {syslogs} ]; then /bin/chmod 755 {syslogs}; fi",
            ],
        )
        pl = self.plist({"command": "x"}, logDir="/var/log/tools")
        self.assertEqual(pl["StandardOutPath"], "/var/log/tools/svc.log")
        pl = self.plist({"command": "x", "logDir": "/l"}, logDir="/var/log/tools")
        self.assertEqual(pl["StandardOutPath"], "/l/svc.log")


class AgentLifecycleTest(LaunchdTestBase):
    def test_create_loads_and_is_idempotent(self):
        raw = self.cfg({"web": {"command": "srv"}})
        self.assertEqual(
            self.diff(raw), [f"  + create user/org.example.web -> {self.agent_path('web')}"]
        )

        self.assertTrue(self.deploy(raw))

        self.assertEqual(self.read(self.agent_path("web"))["Label"], "org.example.web")
        self.assertIn(
            ["/bin/launchctl", "bootstrap", f"gui/{UID}", self.agent_path("web")], self.fake.calls
        )
        self.assertIn("org.example.web", self.fake.loaded)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "logs", "web.log")))
        entry = self.state["launchd"]["services"]["org.example.web"]
        self.assertEqual((entry["domain"], entry["path"]), ("user", self.agent_path("web")))

        self.fake.calls.clear()
        self.assertEqual(self.diff(raw), [])
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.fake.verbs("bootstrap") + self.fake.verbs("bootout"), [])

    def test_semantic_compare_ignores_formatting(self):
        raw = self.cfg({"web": {"command": "srv"}})
        self.deploy(raw)
        pl = self.read(self.agent_path("web"))
        with open(self.agent_path("web"), "wb") as f:
            plistlib.dump(pl, f, fmt=plistlib.FMT_BINARY)
        self.assertEqual(self.diff(raw), [])

    def test_update_writes_diff_and_reloads(self):
        self.deploy(self.cfg({"web": {"command": "srv --old"}}))
        raw = self.cfg({"web": {"command": "srv --new"}})

        with mock.patch("tools.util.shutil.which", return_value=None):
            changes = self.diff(raw)
        self.assertEqual(changes[0], "  ~ update user/org.example.web")
        self.assertIn("srv --new", changes[1])
        self.assertIn("srv --old", changes[1])

        self.fake.calls.clear()
        self.assertTrue(self.deploy(raw))
        verbs = [c[1] for c in self.fake.calls if c[0] == "/bin/launchctl" and c[1] != "print"]
        self.assertEqual(verbs, ["bootout", "bootstrap"])
        self.assertIn("srv --new", self.read(self.agent_path("web"))["ProgramArguments"][2])

    def test_secret_diff_suppressed(self):
        self.deploy(self.cfg({"web": {"command": "srv", "secrets": True}}))
        changes = self.diff(self.cfg({"web": {"command": "srv2", "secrets": True}}))
        self.assertEqual(changes[1], "    (secret, diff suppressed)")

    def test_restart_on_change_false_leaves_running_job(self):
        self.deploy(self.cfg({"web": {"command": "srv"}}))
        raw = self.cfg({"web": {"command": "srv2", "restartOnChange": False}})
        self.assertIn("restartOnChange is false", self.diff(raw)[0])
        self.fake.calls.clear()
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.fake.verbs("bootout"), [])
        self.assertIn("srv2", self.read(self.agent_path("web"))["ProgramArguments"][2])

    def test_not_loaded_job_is_loaded(self):
        raw = self.cfg({"web": {"command": "srv"}})
        self.deploy(raw)
        self.fake.loaded.clear()
        self.assertEqual(self.diff(raw), ["  ~ load user/org.example.web (not loaded)"])
        self.assertTrue(self.deploy(raw))
        self.assertIn("org.example.web", self.fake.loaded)

    def test_removed_label_is_booted_out_and_deleted(self):
        self.deploy(self.cfg({"web": {"command": "srv"}}))
        raw = self.cfg({})
        self.assertTrue(launchd.has_work(raw, self.state))
        self.assertEqual(self.diff(raw), ["  - remove user/org.example.web"])

        self.assertTrue(self.deploy(raw))

        self.assertFalse(os.path.exists(self.agent_path("web")))
        self.assertNotIn("org.example.web", self.fake.loaded)
        self.assertEqual(self.state["launchd"]["services"], {})

    def test_modified_plist_is_not_removed(self):
        self.deploy(self.cfg({"web": {"command": "srv"}}))
        pl = self.read(self.agent_path("web"))
        pl["RunAtLoad"] = False
        with open(self.agent_path("web"), "wb") as f:
            plistlib.dump(pl, f)

        self.assertIn("modified since tools wrote it", self.diff(self.cfg({}))[0])
        self.assertTrue(self.deploy(self.cfg({})))
        self.assertTrue(os.path.exists(self.agent_path("web")))
        self.assertIn("org.example.web", self.fake.loaded)
        self.assertEqual(self.state["launchd"]["services"], {})

    def test_removal_held_while_config_has_errors(self):
        self.deploy(self.cfg({"web": {"command": "srv"}}))
        raw = self.cfg({"broken": {"command": "x", "bogus": True}})
        changes = self.diff(raw)
        self.assertTrue(changes[0].startswith("  ! agents.broken:"))
        self.assertNotIn("  - remove user/org.example.web", changes)
        self.assertFalse(self.deploy(raw))
        self.assertTrue(os.path.exists(self.agent_path("web")))

    def test_dropped_job_already_gone_is_forgotten_silently(self):
        self.deploy(self.cfg({"web": {"command": "srv"}}))
        os.remove(self.agent_path("web"))
        self.fake.loaded.clear()
        self.assertEqual(self.diff(self.cfg({})), [])
        self.assertTrue(self.deploy(self.cfg({})))
        self.assertEqual(self.state["launchd"]["services"], {})


class OwnershipTest(LaunchdTestBase):
    def test_foreign_plist_is_never_replaced(self):
        os.makedirs(self.env.agents_dir)
        with open(self.agent_path("web"), "wb") as f:
            plistlib.dump({"Label": "org.example.web", "Program": "/bin/other"}, f)

        self.assertIn("not managed by tools", self.diff(self.cfg({"web": {"command": "srv"}}))[0])
        self.assertTrue(self.deploy(self.cfg({"web": {"command": "srv"}})))

        self.assertEqual(self.read(self.agent_path("web"))["Program"], "/bin/other")
        self.assertEqual(self.fake.verbs("bootstrap"), [])
        self.assertEqual(self.state["launchd"]["services"], {})

    def test_identical_plist_is_adopted(self):
        raw = self.cfg({"web": {"command": "srv"}})
        self.deploy(raw)
        self.state = {}
        self.assertEqual(self.diff(raw), ["  ~ adopt user/org.example.web (identical plist)"])
        self.assertTrue(self.deploy(raw))
        self.assertIn("org.example.web", self.state["launchd"]["services"])

    def test_untracked_plists_are_never_removed(self):
        os.makedirs(self.env.agents_dir)
        stray = os.path.join(self.env.agents_dir, "org.example.stray.plist")
        with open(stray, "wb") as f:
            plistlib.dump({"Label": "org.example.stray"}, f)
        self.deploy(self.cfg({"web": {"command": "srv"}}))
        self.deploy(self.cfg({}))
        self.assertTrue(os.path.exists(stray))


class LaunchctlEdgeTest(LaunchdTestBase):
    def test_bootout_waits_until_gone(self):
        self.deploy(self.cfg({"web": {"command": "srv"}}))
        self.fake.linger["org.example.web"] = True
        self.fake.script[("print", "org.example.web")] = [0, 0]
        self.assertTrue(self.deploy(self.cfg({"web": {"command": "srv2"}})))
        self.assertIn("org.example.web", self.fake.loaded)
        self.assertGreaterEqual(len(self.fake.verbs("print")), 3)

    def test_bootout_timeout_fails(self):
        self.deploy(self.cfg({"web": {"command": "srv"}}))
        self.env.bootout_polls = 3
        self.fake.script[("bootout", "org.example.web")] = [36]
        self.fake.script[("print", "org.example.web")] = [0] * 10
        self.assertFalse(self.deploy(self.cfg({"web": {"command": "srv2"}})))
        # The plist was written, so it is still tracked and retried next run.
        self.assertIn("org.example.web", self.state["launchd"]["services"])

    def test_bootstrap_retries_on_eio_and_ealready(self):
        for rc in (5, 37):
            with self.subTest(rc):
                self.tearDown()
                self.setUp()
                self.fake.script[("bootstrap", "org.example.web")] = [rc]
                self.assertTrue(self.deploy(self.cfg({"web": {"command": "srv"}})))
                self.assertEqual(len(self.fake.verbs("bootstrap")), 2)
                self.assertIn("org.example.web", self.fake.loaded)

    def test_bootstrap_gives_up_after_attempts(self):
        self.fake.script[("bootstrap", "org.example.web")] = [5, 5, 5]
        self.assertFalse(self.deploy(self.cfg({"web": {"command": "srv"}})))
        self.assertEqual(len(self.fake.verbs("bootstrap")), 3)

    def test_bootstrap_of_launchd_disabled_job_enables_it(self):
        self.fake.disabled.add("org.example.web")
        self.assertTrue(self.deploy(self.cfg({"web": {"command": "srv"}})))
        self.assertIn("org.example.web", self.fake.loaded)

    def test_already_loaded_bootout_is_tolerated_when_not_loaded(self):
        self.assertTrue(self.deploy(self.cfg({"web": {"command": "srv"}})))
        self.assertTrue(any(c[1] == "bootout" for c in self.fake.calls))


class DisabledTest(LaunchdTestBase):
    def test_disabled_keeps_plist_and_disables(self):
        self.deploy(self.cfg({"web": {"command": "srv"}}))
        raw = self.cfg({"web": {"command": "srv", "disabled": True}})
        self.assertEqual(self.diff(raw), ["  ~ disable user/org.example.web"])

        self.assertTrue(self.deploy(raw))

        self.assertTrue(os.path.exists(self.agent_path("web")))
        self.assertIn("org.example.web", self.fake.disabled)
        self.assertNotIn("org.example.web", self.fake.loaded)
        self.assertEqual(self.diff(raw), [])

        enabled = self.cfg({"web": {"command": "srv"}})
        self.assertEqual(self.diff(enabled), ["  ~ enable user/org.example.web"])
        self.assertTrue(self.deploy(enabled))
        self.assertNotIn("org.example.web", self.fake.disabled)
        self.assertIn("org.example.web", self.fake.loaded)

    def test_new_disabled_job_is_written_not_loaded(self):
        self.assertTrue(self.deploy(self.cfg({"web": {"command": "srv", "disabled": True}})))
        self.assertTrue(os.path.exists(self.agent_path("web")))
        self.assertEqual(self.fake.loaded, {})

    def test_disabled_job_loaded_by_hand_is_unloaded(self):
        raw = self.cfg({"web": {"command": "srv", "disabled": True}})
        self.deploy(raw)
        self.fake.loaded["org.example.web"] = self.agent_path("web")
        self.assertEqual(self.diff(raw), ["  ~ unload user/org.example.web (disabled but loaded)"])
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.fake.loaded, {})


class RestartOnTest(LaunchdTestBase):
    def test_kickstarts_when_watched_file_changes(self):
        watched = os.path.join(self.tmp, "Caddyfile")
        with open(watched, "w") as f:
            f.write("v1")
        raw = self.cfg({"caddy": {"command": "caddy run", "restartOn": [watched]}})
        self.deploy(raw)
        self.assertEqual(self.diff(raw), [])

        with open(watched, "w") as f:
            f.write("v2")
        self.assertEqual(
            self.diff(raw), [f"  ~ restart user/org.example.caddy ({watched} changed)"]
        )
        self.fake.calls.clear()
        self.assertTrue(self.deploy(raw))
        self.assertEqual(
            self.fake.verbs("kickstart"),
            [["/bin/launchctl", "kickstart", "-k", f"gui/{UID}/org.example.caddy"]],
        )
        self.assertEqual(self.diff(raw), [])

    def test_appearing_file_restarts_but_newly_listed_does_not(self):
        watched = os.path.join(self.tmp, "secret")
        raw = self.cfg({"svc": {"command": "x", "restartOn": [watched]}})
        self.deploy(raw)
        self.assertIsNone(
            self.state["launchd"]["services"]["org.example.svc"]["restartOn"][watched]
        )
        with open(watched, "w") as f:
            f.write("now here")
        self.assertEqual(self.diff(raw), [f"  ~ restart user/org.example.svc ({watched} changed)"])
        self.deploy(raw)
        self.assertEqual(len(self.fake.verbs("kickstart")), 1)

        other = os.path.join(self.tmp, "other")
        with open(other, "w") as f:
            f.write("x")
        raw = self.cfg({"svc": {"command": "x", "restartOn": [watched, other]}})
        self.assertEqual(self.diff(raw), [])

    def test_unreadable_restart_on_file_is_logged_not_escalated(self):
        watched = os.path.join(self.tmp, "root-only")
        with open(watched, "w") as f:
            f.write("v1")
        real_open = open

        def fake_open(path, *a, **kw):
            if path == watched:
                raise PermissionError(13, "Permission denied")
            return real_open(path, *a, **kw)

        raw = self.cfg(daemons={"d": {"command": "x", "restartOn": [watched]}})
        with (
            mock.patch("builtins.open", side_effect=fake_open),
            mock.patch.object(launchd, "log") as log,
        ):
            self.assertTrue(self.deploy(raw))
        self.assertTrue(any("unreadable" in c.args[0] for c in log.call_args_list))
        self.assertEqual(self.state["launchd"]["services"]["org.example.d"]["restartOn"],
                         {watched: None})  # fmt: skip


class RestartUnitsTest(LaunchdTestBase):
    """restartUnits: templates/sops changes owed to a job, as pending units."""

    def run_units(self, raw, units, daemons=None):
        done = set()
        ok = launchd.install_launchd(raw, self.state, self.env, units=units, done=done,
                                     daemons=daemons)  # fmt: skip
        return ok, done

    def test_loaded_agent_is_kickstarted_once(self):
        raw = self.cfg({"web": {"command": "srv"}})
        self.deploy(raw)
        units = {"org.example.web": ["templates/web"]}
        self.assertEqual(
            launchd.diff_launchd(raw, self.state, self.env, units=units),
            ["  ~ restart user/org.example.web (templates/web changed)"],
        )
        self.fake.calls.clear()
        ok, done = self.run_units(raw, units)
        self.assertTrue(ok)
        self.assertEqual(done, {"org.example.web"})
        self.assertEqual(
            self.fake.verbs("kickstart"),
            [["/bin/launchctl", "kickstart", "-k", f"gui/{UID}/org.example.web"]],
        )

    def test_job_started_this_run_is_not_kickstarted(self):
        raw = self.cfg({"web": {"command": "srv"}})
        ok, done = self.run_units(raw, {"org.example.web": ["templates/web"]})
        self.assertTrue(ok)
        self.assertEqual(done, {"org.example.web"})
        self.assertEqual(self.fake.verbs("kickstart"), [])
        self.assertIn("org.example.web", self.fake.loaded)

    def test_failed_kickstart_stays_pending(self):
        raw = self.cfg({"web": {"command": "srv"}})
        self.deploy(raw)
        self.fake.script[("kickstart", "org.example.web")] = [1]
        ok, done = self.run_units(raw, {"org.example.web": ["templates/web"]})
        self.assertFalse(ok)
        self.assertEqual(done, set())

    def test_restart_on_change_false_still_kickstarts_for_units(self):
        raw = self.cfg({"web": {"command": "srv", "restartOnChange": False}})
        self.deploy(raw)
        raw = self.cfg({"web": {"command": "srv2", "restartOnChange": False}})
        self.fake.calls.clear()
        ok, done = self.run_units(raw, {"org.example.web": ["templates/web"]})
        self.assertTrue(ok)
        self.assertEqual(done, {"org.example.web"})
        self.assertEqual(len(self.fake.verbs("kickstart")), 1)
        self.assertEqual(self.fake.verbs("bootout"), [])

    def test_disabled_agent_has_nothing_to_restart(self):
        raw = self.cfg({"web": {"command": "srv", "disabled": True}})
        self.deploy(raw)
        ok, done = self.run_units(raw, {"org.example.web": ["templates/web"]})
        self.assertTrue(ok)
        self.assertEqual(done, {"org.example.web"})
        self.assertEqual(self.fake.verbs("kickstart"), [])

    def test_daemon_as_user_waits_for_sudo(self):
        raw = self.cfg(daemons={"dns": {"command": "dnsmasq -k"}}, root=False)
        units = {"org.example.dns": ["templates/dnsmasq.conf"]}
        self.assertEqual(
            launchd.diff_launchd(raw, self.state, self.env, units=units),
            [
                "  ~ restart system/org.example.dns (templates/dnsmasq.conf changed; "
                "under sudo tools deploy)"
            ],
        )
        with mock.patch.object(launchd, "log") as log:
            ok, done = self.run_units(raw, units)
        self.assertTrue(ok)
        self.assertEqual(done, set())
        self.assertTrue(any("waits for sudo" in c.args[0] for c in log.call_args_list))
        self.assertEqual(self.fake.system_calls, [])

    def test_disabled_daemon_as_user_is_not_owed(self):
        raw = self.cfg(daemons={"dns": {"command": "x", "disabled": True}}, root=False)
        units = {"org.example.dns": ["templates/x"]}
        self.assertEqual(launchd.diff_launchd(raw, self.state, self.env, units=units), [])
        queue = []
        ok, done = self.run_units(raw, units, daemons=queue)
        self.assertEqual((ok, done, queue), (True, {"org.example.dns"}, []))

    def test_adopted_job_restart_sets_up_its_logs(self):
        raw = self.cfg({"web": {"command": "srv"}})
        self.deploy(raw)
        self.state = {}
        with mock.patch.object(launchd, "_ensure_logs", return_value=True) as logs:
            ok, done = self.run_units(raw, {"org.example.web": ["templates/web"]})
        self.assertTrue(ok)
        self.assertEqual(done, {"org.example.web"})
        logs.assert_called()
        self.assertEqual(len(self.fake.verbs("kickstart")), 1)

    def test_daemon_as_sudo_child_is_queued_for_root(self):
        raw = self.cfg(daemons={"dns": {"command": "dnsmasq -k"}}, root=False)
        queue = []
        ok, done = self.run_units(raw, {"org.example.dns": ["templates/x"]}, daemons=queue)
        self.assertTrue(ok)
        # Owed until the root parent has restarted it.
        self.assertEqual((queue, done), (["org.example.dns"], set()))
        self.assertEqual(self.fake.system_calls, [])

    def test_restart_daemons_as_root(self):
        raw = self.cfg(daemons={"dns": {"command": "dnsmasq -k"}, "off": {"command": "x"}})
        self.assertTrue(self.deploy(raw))
        self.fake.calls.clear()
        labels = ["org.example.dns", "org.example.dns", "org.example.nope"]
        done = set()
        self.assertTrue(launchd.restart_daemons(raw, labels, self.state, self.env, done=done))
        self.assertEqual(done, {"org.example.dns", "org.example.nope"})
        self.assertEqual(
            self.fake.verbs("kickstart"),
            [["/bin/launchctl", "kickstart", "-k", "system/org.example.dns"]],
        )

    def test_failed_daemon_kickstart_stays_owed(self):
        raw = self.cfg(daemons={"dns": {"command": "dnsmasq -k"}})
        self.deploy(raw)
        self.fake.script[("kickstart", "org.example.dns")] = [1]
        done = set()
        with mock.patch.object(launchd, "log"):
            ok = launchd.restart_daemons(raw, ["org.example.dns"], self.state, self.env, done=done)
        self.assertFalse(ok)
        self.assertEqual(done, set())

    def test_restart_daemons_holds_labels_while_config_has_errors(self):
        raw = self.cfg(daemons={"dns": {"command": "dnsmasq -k"}})
        self.deploy(raw)
        broken = self.cfg(daemons={"dns": {"command": "dnsmasq -k", "bogus": 1}})
        done = set()
        with mock.patch.object(launchd, "log"):
            launchd.restart_daemons(broken, ["org.example.dns"], self.state, self.env, done=done)
        self.assertEqual(done, set())
        self.assertEqual(self.fake.verbs("kickstart"), [])

    def test_restart_daemons_skips_untracked_unloaded_and_disabled(self):
        raw = self.cfg(daemons={"dns": {"command": "dnsmasq -k"}})
        self.assertTrue(launchd.restart_daemons(raw, ["org.example.dns"], {}, self.env))
        self.deploy(raw)
        del self.fake.loaded["org.example.dns"]
        self.assertTrue(launchd.restart_daemons(raw, ["org.example.dns"], self.state, self.env))
        off = self.cfg(daemons={"dns": {"command": "dnsmasq -k", "disabled": True}})
        self.assertTrue(launchd.restart_daemons(off, ["org.example.dns"], self.state, self.env))
        self.assertEqual(self.fake.verbs("kickstart"), [])

    def test_restart_daemons_does_nothing_as_user(self):
        raw = self.cfg(daemons={"dns": {"command": "dnsmasq -k"}}, root=False)
        self.assertTrue(launchd.restart_daemons(raw, ["org.example.dns"], {}, self.env))
        self.assertEqual(self.fake.calls, [])

    def test_stale_units(self):
        raw = self.cfg({"web": {"command": "srv"}})
        units = {"org.example.web": ["a"], "org.example.gone": ["b"]}
        with mock.patch.object(launchd, "log"):
            self.assertEqual(launchd.stale_units(raw, units, self.env), {"org.example.gone"})
        broken = self.cfg({"web": {"command": "srv"}, "bad": {}})
        self.assertEqual(launchd.stale_units(broken, units, self.env), set())
        self.env.is_darwin = False
        self.assertEqual(launchd.stale_units(raw, units, self.env), set(units))


class SystemDomainTest(LaunchdTestBase):
    def daemon(self, root=None, **extra):
        return self.cfg(daemons={"dns": {"command": "dnsmasq -k", **extra}}, root=root)

    def test_user_mode_leaves_daemons_alone_with_a_note(self):
        raw = self.daemon(root=False)
        with mock.patch.object(launchd, "log") as log:
            self.assertEqual(self.diff(raw), [])
            self.assertTrue(self.deploy(raw))
        notes = [c.args[0] for c in log.call_args_list]
        self.assertIn(
            "launchd: 1 daemon(s) not managed as user; run sudo tools deploy --scope launchd",
            notes,
        )
        self.assertFalse(os.path.exists(self.daemon_path("dns")))
        self.assertEqual(self.fake.system_calls, [])
        self.assertEqual(self.state["launchd"]["services"], {})

    def test_sudo_skips_agents(self):
        raw = self.cfg({"web": {"command": "srv"}}, {"dns": {"command": "dnsmasq -k"}})
        self.assertEqual(
            self.diff(raw), [f"  + create system/org.example.dns -> {self.daemon_path('dns')}"]
        )
        self.assertTrue(self.deploy(raw))
        self.assertFalse(os.path.exists(self.agent_path("web")))
        self.assertEqual(list(self.state["launchd"]["services"]), ["org.example.dns"])
        self.assertFalse(any(f"gui/{UID}" in " ".join(c) for c in self.fake.calls))

    def test_root_without_sudo_user_refuses(self):
        raw = self.daemon()
        self.env.uid = 0
        self.assertEqual(self.diff(raw), [f"  ! {launchd.privilege.SUDO_USER_ERROR}"])
        self.assertFalse(self.deploy(raw))
        self.assertEqual(self.fake.calls, [])
        with self.assertRaises(PermissionError):
            launchd.status_rows(raw, self.state, self.env)

    def test_root_writes_and_launchctl_run_directly(self):
        raw = self.daemon(user="svc")
        self.assertEqual(
            self.diff(raw),
            [f"  + create system/org.example.dns -> {self.daemon_path('dns')}"],
        )
        self.assertTrue(self.deploy(raw))

        self.assertTrue(os.path.exists(self.daemon_path("dns")))
        self.assertIn(
            ["/bin/launchctl", "bootstrap", "system", self.daemon_path("dns")],
            self.fake.calls,
        )
        install = [c for c in self.fake.calls if "/usr/bin/install" in c and "-S" in c][0]
        self.assertEqual(
            install[:8],
            ["/usr/bin/install", "-S", "-m", "0644", "-o", "root", "-g", "wheel"],
        )
        self.assertIn(["/usr/sbin/chown", "-h", "svc:staff",
                       os.path.join(self.env.system_log_dir, "dns.log"),
                       os.path.join(self.env.system_log_dir, "dns.error.log")],
                      self.fake.calls)  # fmt: skip
        self.assertTrue(self.deploy(self.cfg({}, root=True)))
        self.assertFalse(os.path.exists(self.daemon_path("dns")))
        self.assertIn(["/bin/rm", "-f", self.daemon_path("dns")], self.fake.calls)

    def test_moving_an_agent_to_daemons_is_two_runs_with_two_states(self):
        user_state = {}
        launchd.install_launchd(self.cfg({"dns": {"command": "dnsmasq -k"}}), user_state, self.env)
        moved = self.cfg(daemons={"dns": {"command": "dnsmasq -k"}}, root=False)
        self.assertEqual(launchd.diff_launchd(moved, user_state, self.env),
                         ["  - remove user/org.example.dns"])  # fmt: skip
        self.assertTrue(launchd.install_launchd(moved, user_state, self.env))
        self.assertFalse(os.path.exists(self.agent_path("dns")))
        self.assertEqual(user_state["launchd"]["services"], {})

        self.as_root()
        root_state = {}
        self.assertTrue(launchd.install_launchd(moved, root_state, self.env))
        self.assertTrue(os.path.exists(self.daemon_path("dns")))
        self.assertEqual(root_state["launchd"]["services"]["org.example.dns"]["domain"], "system")

    def test_other_domain_state_entries_are_left_alone(self):
        self.deploy(self.cfg({"dns": {"command": "dnsmasq -k"}}))
        self.as_root()
        self.fake.calls.clear()
        self.assertEqual(self.diff(self.cfg({})),
                         ["  ? org.example.dns: user job in this state; left alone"])  # fmt: skip
        self.assertTrue(self.deploy(self.cfg({})))
        self.assertTrue(os.path.exists(self.agent_path("dns")))
        self.assertIn("org.example.dns", self.state["launchd"]["services"])
        self.assertEqual(self.fake.system_calls, [])

    def test_daemon_temp_file_is_private_and_beside_the_plist(self):
        modes = []
        real = self.fake.run

        def run(cmd):
            if cmd[0] == "/usr/bin/install":
                modes.append((os.path.dirname(cmd[-2]), os.stat(cmd[-2]).st_mode & 0o777))
            return real(cmd)

        self.env.run = run
        with mock.patch.dict(os.environ, {"TMPDIR": os.path.join(self.tmp, "usertmp")}):
            self.assertTrue(self.deploy(self.daemon()))
        self.assertEqual(modes, [(self.env.daemons_dir, 0o600)])
        self.assertEqual(os.listdir(self.env.daemons_dir), ["org.example.dns.plist"])

    def test_symlinked_daemons_dir_is_refused(self):
        target = os.path.join(self.tmp, "elsewhere")
        os.makedirs(target)
        os.symlink(target, self.env.daemons_dir)
        self.assertFalse(self.deploy(self.daemon()))
        self.assertEqual(os.listdir(target), [])

    def test_root_never_writes_under_the_users_home(self):
        home = self.env.home = os.path.join(self.tmp, "home")
        self.env.daemons_dir = os.path.join(home, "LaunchDaemons")
        wd = os.path.join(home, "work")
        raw = self.cfg(
            daemons={"a": {"command": "x"}, "b": {"command": "y", "workingDirectory": wd}}
        )
        changes = self.diff(raw)
        plist_a = os.path.join(home, "LaunchDaemons", "org.example.a.plist")
        self.assertIn(
            f"  ! a: plist {plist_a} is under {home}; root never writes in user space", changes
        )
        self.assertIn(
            f"  ! b: workingDirectory {wd} is under {home}; root never writes in user space",
            changes,
        )
        self.assertFalse(self.deploy(raw))
        self.assertFalse(os.path.exists(home))
        self.assertEqual(self.fake.system_calls, [])

    def test_root_writes_other_daemons_when_one_targets_the_home(self):
        self.as_root()
        home = self.env.home
        raw = self.cfg(
            daemons={
                "bad": {"command": "x", "workingDirectory": os.path.join(home, "w")},
                "good": {"command": "y"},
            }
        )
        self.assertFalse(self.deploy(raw))
        self.assertTrue(os.path.exists(self.daemon_path("good")))
        self.assertFalse(os.path.exists(self.daemon_path("bad")))

    def test_existing_working_directory_under_the_home_is_allowed(self):
        self.as_root()
        wd = os.path.join(self.env.home, "w")
        os.makedirs(wd)
        raw = self.cfg(daemons={"d": {"command": "x", "user": "me", "workingDirectory": wd}})
        self.assertEqual(
            self.diff(raw), [f"  + create system/org.example.d -> {self.daemon_path('d')}"]
        )
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.read(self.daemon_path("d"))["WorkingDirectory"], wd)
        self.assertFalse(any(wd in c for c in self.fake.system_calls if c[0] == "/bin/mkdir"))

    def test_missing_working_directory_under_the_home_is_never_created(self):
        self.as_root()
        wd = os.path.join(self.env.home, "w")
        raw = self.cfg(daemons={"d": {"command": "x", "workingDirectory": wd}})
        self.assertIn(
            f"  ! d: workingDirectory {wd} is under {self.env.home}; "
            "root never writes in user space",
            self.diff(raw),
        )
        self.assertFalse(self.deploy(raw))
        self.assertFalse(os.path.exists(wd))
        self.assertFalse(os.path.exists(self.daemon_path("d")))


class StatusAndPlatformTest(LaunchdTestBase):
    def test_status_rows(self):
        self.deploy(self.cfg({"web": {"command": "srv"}}))
        rows = launchd.status_rows(
            self.cfg({"web": {"command": "srv"}, "off": {"command": "x"}}), self.state, self.env
        )
        by_label = {label: st for label, _d, st in rows}
        self.assertEqual(by_label["org.example.web"].pid, 42)
        self.assertEqual(by_label["org.example.web"].last_exit, "0")
        self.assertFalse(by_label["org.example.off"].loaded)

    def test_non_darwin_is_skipped(self):
        self.env.is_darwin = False
        raw = self.cfg({"web": {"command": "srv"}})
        self.assertEqual(self.diff(raw), [])
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.state, {})


class ReviewRegressionTest(LaunchdTestBase):
    # 2/13: the command is always exec'd, exactly like nix-config's `exec ${command}`.
    def test_command_is_execed_like_nix(self):
        for command in ("/opt/bin/srv --port 1", "FOO=1 srv", "myfunc"):
            with self.subTest(command):
                svc = launchd.Service(name="s", label="org.example.s", command=command)
                last = launchd._script(svc).rstrip("\n").splitlines()[-1]
                self.assertEqual(last, f"exec {command}")
        _s, svcs, errors = launchd.parse_launchd(self.cfg({"s": {"command": "srv", "exec": True}}))
        self.assertEqual(svcs, [])
        self.assertIn("unknown keys ['exec']", errors[0])

    # 4: secret plists are not world-readable.
    def test_secret_plist_mode_0600(self):
        raw = self.cfg({"web": {"command": "srv", "environment": {"TOKEN": "x"}}})
        self.deploy(raw)
        self.assertEqual(os.stat(self.agent_path("web")).st_mode & 0o777, 0o644)
        secret = self.cfg(
            {"web": {"command": "srv", "environment": {"TOKEN": "x"}, "secrets": True}}
        )
        self.assertEqual(self.diff(secret), ["  ~ chmod user/org.example.web -> 0600"])
        self.deploy(secret)
        self.assertEqual(os.stat(self.agent_path("web")).st_mode & 0o777, 0o600)
        self.assertEqual(self.diff(secret), [])

    def test_secret_daemon_installed_0600(self):
        raw = self.cfg(daemons={"d": {"command": "x", "secrets": True}})
        self.assertTrue(self.deploy(raw))
        install = [c for c in self.fake.calls if "/usr/bin/install" in c and "-S" in c][0]
        self.assertEqual(install[2:4], ["-m", "0600"])

    # 5: nothing is created under an unmounted volume.
    def test_logs_not_created_on_unmounted_volume(self):
        self.env.ismount = lambda _p: False
        log_dir = os.path.join(self.env.volumes_root, "Storage", "logs")
        self.assertTrue(self.deploy(self.cfg({"web": {"command": "srv", "logDir": log_dir}})))
        self.assertFalse(os.path.exists(self.env.volumes_root))

        self.env.ismount = lambda _p: True
        self.deploy(self.cfg({"web": {"command": "srv", "logDir": log_dir}}))
        self.assertTrue(os.path.exists(os.path.join(log_dir, "web.log")))

    def test_dirs_are_created_without_mount_waits_like_nix(self):
        svc = launchd.Service(
            name="s",
            label="org.example.s",
            command="srv",
            wait_for_paths=["/Volumes/Storage"],
            data_dir="/Volumes/Other/.s",
            extra_dirs=[launchd.ExtraDir("/Volumes/Storage/x")],
        )
        script = launchd._script(svc, "/Volumes/Logs")
        self.assertNotIn("stat -f", script)
        self.assertIn("while [ ! -e /Volumes/Storage ]", script)
        self.assertIn("/bin/mkdir -p /Volumes/Other/.s\n/bin/mkdir -p /Volumes/Storage/x", script)
        self.assertIn("/bin/mkdir -p /Volumes/Logs", script)
        self.assertIsNone(launchd.volume_of("/Users/x", "/Volumes"))

    def test_bootstrap_119_on_only_attempt_still_bootstraps(self):
        self.env.bootstrap_attempts = 1
        self.fake.disabled.add("org.example.web")
        self.assertTrue(self.deploy(self.cfg({"web": {"command": "srv"}})))
        self.assertIn("org.example.web", self.fake.loaded)
        self.assertEqual(len(self.fake.verbs("bootstrap")), 2)

    def test_bootstrap_failures_always_log(self):
        for script in ([119, 119], [1], [5, 5, 5]):
            with self.subTest(script):
                self.fake.script[("bootstrap", "org.example.web")] = list(script)
                with mock.patch.object(launchd, "log") as log:
                    ok = launchd.bootstrap(
                        "org.example.web", "user", "/x/org.example.web.plist", self.env
                    )
                self.assertFalse(ok)
                self.assertTrue(
                    any("bootstrap org.example.web failed" in c.args[0] for c in log.call_args_list)
                )

    # 8: an adopted job is recorded only once its action succeeded.
    def test_adopted_disabled_job_not_recorded_when_disable_fails(self):
        raw = self.cfg({"web": {"command": "srv", "disabled": True}})
        self.deploy(raw)
        self.state = {}
        self.fake.disabled.discard("org.example.web")
        self.fake.script[("disable", "org.example.web")] = [1]

        self.assertFalse(self.deploy(raw))
        self.assertNotIn("org.example.web", self.state["launchd"]["services"])
        self.assertEqual(self.diff(raw), ["  ~ disable user/org.example.web"])

    def test_daemon_not_adopted_as_user(self):
        raw = self.cfg(daemons={"d": {"command": "x", "disabled": True}})
        self.deploy(raw)
        self.fake.disabled.discard("org.example.d")
        self.state = {}
        self.as_user()
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.state["launchd"]["services"], {})
        self.assertNotIn("org.example.d", self.fake.disabled)

    # 9: restartOn still fires when restartOnChange is false and the plist changed too.
    def test_restart_on_honoured_with_restart_on_change_false(self):
        watched = os.path.join(self.tmp, "conf")
        with open(watched, "w") as f:
            f.write("v1")
        self.deploy(self.cfg({"s": {"command": "srv", "restartOn": [watched]}}))
        with open(watched, "w") as f:
            f.write("v2")
        raw = self.cfg({"s": {"command": "srv2", "restartOn": [watched], "restartOnChange": False}})
        self.assertIn(f"(kickstart: {watched} changed)", self.diff(raw)[0])
        self.fake.calls.clear()

        self.assertTrue(self.deploy(raw))

        self.assertEqual(len(self.fake.verbs("kickstart")), 1)
        self.assertEqual(self.fake.verbs("bootout"), [])
        self.assertEqual(self.diff(raw), [])

    # 10: a failed activation is retried even though the plist is current.
    def test_failed_activation_is_retried_next_run(self):
        raw = self.cfg({"web": {"command": "srv"}})
        self.fake.script[("bootstrap", "org.example.web")] = [1]
        self.assertFalse(self.deploy(raw))
        self.assertTrue(self.state["launchd"]["services"]["org.example.web"]["pending"])

        self.assertEqual(
            self.diff(raw), ["  ~ retry user/org.example.web (last activation failed)"]
        )
        self.assertTrue(self.deploy(raw))
        self.assertIn("org.example.web", self.fake.loaded)
        self.assertNotIn("pending", self.state["launchd"]["services"]["org.example.web"])
        self.assertEqual(self.diff(raw), [])

    def test_failed_disable_is_retried_next_run(self):
        self.deploy(self.cfg({"web": {"command": "srv"}}))
        raw = self.cfg({"web": {"command": "srv2", "disabled": True}})
        self.fake.script[("disable", "org.example.web")] = [1]
        self.assertFalse(self.deploy(raw))
        entry = self.state["launchd"]["services"]["org.example.web"]
        self.assertEqual((entry["pending"], entry["disabled"]), ("activate", False))

        self.assertTrue(self.deploy(raw))
        self.assertIn("org.example.web", self.fake.disabled)
        self.assertNotIn("org.example.web", self.fake.loaded)
        self.assertEqual(self.diff(raw), [])


class RoundThreeTest(LaunchdTestBase):
    def daemon_cfg(self, root=True, **extra):
        svc = {"command": "dnsmasq -k", **extra}
        return self.cfg(daemons={"dns": svc}, root=root)

    def lock(self, path):
        """Make path unreadable to tools (a root-owned 0600 plist) without changing its mode."""
        real = launchd.read_plist

        def read(p):
            return (True, None, False) if p == path else real(p)

        patcher = mock.patch.object(launchd, "read_plist", side_effect=read)
        patcher.start()
        self.addCleanup(patcher.stop)

    # 11: an unreadable plist is unknown, never rewritten blindly or read with escalation.
    def test_unreadable_plist_is_unknown_and_untouched(self):
        raw = self.daemon_cfg(secrets=True)
        self.deploy(raw)
        before = dict(self.state["launchd"]["services"])
        self.lock(self.daemon_path("dns"))
        self.fake.calls.clear()
        self.fake.system_calls.clear()

        changes = self.diff(raw)
        self.assertEqual(
            changes,
            [
                f"  ? org.example.dns: unknown, {self.daemon_path('dns')} unreadable; "
                "check its owner and permissions"
            ],
        )
        self.assertFalse(self.deploy(raw))
        self.assertEqual(self.state["launchd"]["services"], before)
        self.assertEqual(self.fake.system_calls, [])

    # 12: unreadable is never "modified": removal holds, state stays.
    def test_unreadable_plist_removal_is_held(self):
        self.deploy(self.daemon_cfg(secrets=True))
        before = dict(self.state["launchd"]["services"])
        self.lock(self.daemon_path("dns"))
        raw = self.cfg({}, root=True)
        self.assertIn("unknown", self.diff(raw)[0])
        self.deploy(raw)
        self.assertEqual(self.state["launchd"]["services"], before)
        self.assertIn("org.example.dns", self.fake.loaded)

    # 14: `~` in user-domain paths is the login user's home, not root's.
    def test_tilde_uses_login_user_home(self):
        agents = {
            "u": {
                "command": "x",
                "logDir": "~/logs",
                "workingDirectory": "~/w",
                "waitForPaths": ["~/ready"],
                "restartOn": ["~/conf"],
                "keepAlive": {"pathState": {"~/flag": True}},
                "extraDirs": ["~/d"],
            },
        }
        daemons = {
            "s": {"command": "x", "workingDirectory": "~/w"},
            "m": {"command": "x", "user": "me", "workingDirectory": "~/w"},
        }
        _s, svcs, errors = launchd.parse_launchd(
            self.cfg(agents, daemons), home="/Users/someone", login="me"
        )
        self.assertEqual(len(errors), 1)
        self.assertTrue(
            errors[0].startswith("daemons.s: workingDirectory: '~/w' would be expanded")
        )
        u, sys_svc = {v.name: v for v in svcs}["u"], {v.name: v for v in svcs}["m"]
        self.assertEqual(u.log_dir, "/Users/someone/logs")
        self.assertEqual(u.working_directory, "/Users/someone/w")
        self.assertEqual(u.wait_for_paths, ["/Users/someone/ready"])
        self.assertEqual(u.restart_on, ["/Users/someone/conf"])
        self.assertEqual(u.keep_alive, {"PathState": {"/Users/someone/flag": True}})
        self.assertEqual(u.extra_dirs[0].path, "/Users/someone/d")
        self.assertEqual(sys_svc.working_directory, "/Users/someone/w")

    def test_deploy_expands_tilde_with_env_home(self):
        self.env.home = self.tmp
        self.deploy(self.cfg({"web": {"command": "srv", "stdoutPath": "~/o.log"}}))
        self.assertEqual(self.read(self.agent_path("web"))["StandardOutPath"], f"{self.tmp}/o.log")

    # 18: as the user nothing touches daemons, from any call site.
    def test_no_daemon_commands_from_log_upkeep_as_user(self):
        self.deploy(self.daemon_cfg())
        self.fake.calls.clear()
        self.fake.system_calls.clear()
        self.deploy(self.daemon_cfg(root=False))
        self.assertEqual(self.fake.system_calls, [])

    def test_gate_refuses_the_other_runs_domain(self):
        for root, domain, hint in ((False, "system", "sudo tools deploy"),
                                   (True, "user", "run without sudo")):  # fmt: skip
            with self.subTest(domain=domain):
                self.as_root() if root else self.as_user()
                rc, _out, err = launchd._run_changing(["/bin/rm", "-f", "/x"], domain, self.env)
                self.assertEqual(rc, 1)
                self.assertIn(hint, err)
                self.assertEqual(self.fake.calls, [])

    # 19: bootstrap EIO on a launchd-disabled job enables it (only when config wants it on).
    def test_bootstrap_eio_on_disabled_job_enables_and_retries(self):
        self.fake.disabled_rc = 5
        self.fake.disabled.add("org.example.web")
        self.assertTrue(self.deploy(self.cfg({"web": {"command": "srv"}})))
        self.assertIn("org.example.web", self.fake.loaded)
        self.assertNotIn("pending", self.state["launchd"]["services"]["org.example.web"])

    def test_config_disabled_job_is_never_enabled(self):
        self.fake.disabled_rc = 5
        self.fake.disabled.add("org.example.web")
        self.deploy(self.cfg({"web": {"command": "srv", "disabled": True}}))
        self.assertEqual(self.fake.verbs("enable"), [])
        self.assertIn("org.example.web", self.fake.disabled)


class StepRecordingTest(LaunchdTestBase):
    # 20: adopting enforces mode and owner in the same run.
    def test_adopted_secret_plist_gets_0600_now(self):
        raw = self.cfg({"web": {"command": "srv", "secrets": True}})
        self.deploy(raw)
        os.chmod(self.agent_path("web"), 0o644)
        self.state = {}
        self.assertEqual(
            self.diff(raw),
            [
                "  ~ adopt user/org.example.web (identical plist)",
                "  ~ chmod user/org.example.web -> 0600",
            ],
        )
        self.assertTrue(self.deploy(raw))
        self.assertEqual(os.stat(self.agent_path("web")).st_mode & 0o777, 0o600)
        self.assertIn("org.example.web", self.state["launchd"]["services"])

    def test_adopted_daemon_owner_and_mode_enforced(self):
        raw = self.cfg(daemons={"d": {"command": "x", "secrets": True}})
        self.deploy(raw)
        os.chmod(self.daemon_path("d"), 0o644)
        self.state = {}
        self.fake.calls.clear()
        self.fake.system_calls.clear()
        self.assertTrue(self.deploy(raw))
        reinstall = [c for c in self.fake.system_calls if c[:2] == ["/usr/bin/install", "-S"]]
        self.assertEqual(len(reinstall), 1)
        self.assertEqual(reinstall[0][2:8], ["-m", "0600", "-o", "root", "-g", "wheel"])
        self.assertEqual(reinstall[0][-1], self.daemon_path("d"))
        self.assertEqual(os.stat(self.daemon_path("d")).st_mode & 0o777, 0o600)
        self.assertFalse(
            any(c[0] in ("/bin/chmod", "/usr/sbin/chown") and self.daemon_path("d") in c
                for c in self.fake.system_calls)
        )  # fmt: skip

    # 21: a failed restartOn kickstart is retried as a kickstart, never a reload.
    def test_failed_kickstart_retried_as_kickstart(self):
        watched = os.path.join(self.tmp, "conf")
        with open(watched, "w") as f:
            f.write("v1")
        self.deploy(self.cfg({"s": {"command": "srv", "restartOn": [watched]}}))
        with open(watched, "w") as f:
            f.write("v2")
        raw = self.cfg({"s": {"command": "srv2", "restartOn": [watched], "restartOnChange": False}})
        self.fake.script[("kickstart", "org.example.s")] = [1]
        self.assertFalse(self.deploy(raw))
        self.assertNotIn("pending", self.state["launchd"]["services"]["org.example.s"])

        self.assertEqual(self.diff(raw), [f"  ~ restart user/org.example.s ({watched} changed)"])
        self.fake.calls.clear()
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.fake.verbs("bootout") + self.fake.verbs("bootstrap"), [])
        self.assertEqual(len(self.fake.verbs("kickstart")), 1)

    # 22: retry runs the same pre-activation steps (logs) as a normal apply.
    def test_retry_creates_logs_once_volume_mounts(self):
        log_dir = os.path.join(self.env.volumes_root, "Storage", "logs")
        raw = self.cfg({"web": {"command": "srv", "logDir": log_dir}})
        self.env.ismount = lambda _p: False
        self.fake.script[("bootstrap", "org.example.web")] = [1]
        self.assertFalse(self.deploy(raw))

        self.env.ismount = lambda _p: True
        self.assertTrue(self.deploy(raw))
        self.assertTrue(os.path.exists(os.path.join(log_dir, "web.log")))

    # 23: an override set by _set_enabled is recorded even when the next step fails.
    def test_disable_override_recorded_when_bootout_fails_and_config_reverts(self):
        self.deploy(self.cfg({"web": {"command": "srv"}}))
        self.fake.script[("bootout", "org.example.web")] = [1]
        self.fake.script[("print", "org.example.web")] = [0]
        self.assertFalse(self.deploy(self.cfg({"web": {"command": "srv", "disabled": True}})))
        entry = self.state["launchd"]["services"]["org.example.web"]
        self.assertEqual((entry["disabled"], entry["pending"]), (True, "activate"))

        enabled = self.cfg({"web": {"command": "srv"}})
        self.assertEqual(
            self.diff(enabled), ["  ~ retry user/org.example.web (last activation failed)"]
        )
        self.assertTrue(self.deploy(enabled))
        self.assertNotIn("org.example.web", self.fake.disabled)
        self.assertIn("org.example.web", self.fake.loaded)
        self.assertEqual(self.diff(enabled), [])

    def test_enable_override_recorded_when_bootstrap_fails(self):
        self.deploy(self.cfg({"web": {"command": "srv", "disabled": True}}))
        self.fake.script[("bootstrap", "org.example.web")] = [1]
        self.assertFalse(self.deploy(self.cfg({"web": {"command": "srv"}})))
        entry = self.state["launchd"]["services"]["org.example.web"]
        self.assertEqual((entry["disabled"], entry["pending"]), (False, "activate"))
        self.assertNotIn("org.example.web", self.fake.disabled)


class PrivilegedPathTest(LaunchdTestBase):
    def daemon_cfg(self, **extra):
        svc = {"command": "x", **extra}
        return self.cfg(daemons={"d": svc}, root=True)

    def test_shared_log_path_is_a_config_error(self):
        shared = os.path.join(self.tmp, "shared.log")
        raw = self.cfg(
            {
                "a": {"command": "x", "stdoutPath": shared},
                "b": {"command": "y", "stdoutPath": shared},
            }
        )
        changes = self.diff(raw)
        self.assertEqual(len([c for c in changes if "is shared" in c]), 2)
        self.assertFalse(self.deploy(raw))
        self.assertEqual(self.fake.verbs("bootstrap"), [])

    def test_open_regular_refuses_symlinks(self):
        target = os.path.join(self.tmp, "t")
        open(target, "w").close()
        link = os.path.join(self.tmp, "l")
        os.symlink(target, link)
        with self.assertRaises(OSError):
            launchd._open_regular(link, os.O_WRONLY | os.O_APPEND)
        with self.assertRaises(OSError):
            launchd._open_regular(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL)

    # 27: fixing plist permissions never goes through a symlink.
    def test_fix_perms_refuses_symlinked_plist(self):
        raw = self.cfg({"web": {"command": "srv", "secrets": True}})
        self.deploy(raw)
        real = os.path.join(self.tmp, "real.plist")
        os.replace(self.agent_path("web"), real)
        os.chmod(real, 0o644)
        os.symlink(real, self.agent_path("web"))
        self.state = {}

        self.assertFalse(self.deploy(raw))
        self.assertEqual(os.stat(real).st_mode & 0o777, 0o644)
        self.assertNotIn("org.example.web", self.state["launchd"]["services"])

    def test_fix_perms_as_root_refuses_symlink(self):
        item = launchd.Item(
            svc=launchd.Service(name="d", label="org.example.d", domain="system", command="x"),
            path=os.path.join(self.tmp, "link.plist"),
            plist={},
            data=b"",
            hash="",
            restart_hashes={},
        )
        os.symlink(os.path.join(self.tmp, "x"), item.path)
        self.as_root()
        self.assertFalse(launchd._fix_perms(item, self.env))
        self.assertEqual(self.fake.calls, [])

    def test_restart_on_symlink_is_followed(self):
        target = os.path.join(self.tmp, "secret")
        with open(target, "w") as f:
            f.write("s")
        link = os.path.join(self.tmp, "conf")
        os.symlink(target, link)
        self.assertEqual(launchd._file_hashes([link])[link], launchd._file_hashes([target])[target])
        self.assertIsNotNone(launchd._file_hashes([link])[link])

    # 28: names and labels cannot escape their directories.
    def test_unsafe_names_and_labels_rejected(self):
        for name, svc in (
            ("../../x", {"command": "x", "label": "org.example.ok"}),
            ("a/b", {"command": "x", "label": "org.example.ok"}),
            ("..", {"command": "x", "label": "org.example.ok"}),
            ("ok", {"command": "x", "label": "org..example"}),
            ("ok", {"command": "x", "label": "a/b"}),
        ):
            with self.subTest(name=name, label=svc["label"]):
                _s, svcs, errors = launchd.parse_launchd(self.cfg({name: svc}))
                self.assertEqual(svcs, [])
                self.assertTrue(errors)

    def test_derived_paths_must_stay_inside(self):
        with self.assertRaises(ValueError):
            launchd._inside("/a/logs", "../x.log")
        svc = launchd.Service(name="../x", label="org.example.x", command="x")
        p = launchd.plan(launchd.Settings(), [svc], {}, self.env)
        self.assertTrue(any("escape" in e for e in p.errors), p.errors)


class NoRootTest(LaunchdTestBase):
    def daemon_cfg(self, root=True, **extra):
        svc = {"command": "dnsmasq -k", **extra}
        return self.cfg(daemons={"dns": svc}, root=root)

    def test_refuses_root_without_a_user_to_act_for(self):
        raw = self.cfg({"web": {"command": "srv"}})
        for euid in (0, UID):
            with self.subTest(euid=euid):
                self.env.euid, self.env.uid = euid, 0
                self.assertFalse(self.deploy(raw))
                self.assertEqual(self.diff(raw), [f"  ! {launchd.privilege.SUDO_USER_ERROR}"])
                with self.assertRaises(PermissionError):
                    launchd.status_rows(raw, self.state, self.env)
                self.assertEqual(self.fake.calls, [])
                self.assertEqual(self.state, {})
                self.assertFalse(os.path.exists(self.env.agents_dir))
        self.assertIn("must not run as root directly", launchd.privilege.SUDO_USER_ERROR)

    def test_root_runs_only_fixed_tools_directly(self):
        watched = os.path.join(self.tmp, "conf")
        with open(watched, "w") as f:
            f.write("v1")
        self.deploy(self.daemon_cfg(restartOn=[watched]))
        with open(watched, "w") as f:
            f.write("v2")
        self.deploy(self.daemon_cfg(restartOn=[watched]))
        self.deploy(self.daemon_cfg(command="dnsmasq -d"))
        self.deploy(self.daemon_cfg(disabled=True))
        self.deploy(self.daemon_cfg())
        self.deploy(self.cfg({}, root=True))
        calls = self.fake.system_calls
        self.assertTrue(calls)
        allowed = {"/usr/bin/install", launchd.LAUNCHCTL, "/bin/rm", "/usr/sbin/chown",
                   "/bin/mkdir", "/usr/bin/touch"}  # fmt: skip
        for c in self.fake.calls:
            self.assertIn(c[0], allowed, c)
            self.assertFalse(any("sudo" in arg for arg in c), c)
        verbs = {c[1] for c in calls if c[0] == launchd.LAUNCHCTL}
        self.assertEqual(verbs, {"bootout", "bootstrap", "kickstart", "disable", "enable"})

    def test_exact_argv_for_system_create_and_removal(self):
        path = self.daemon_path("dns")
        self.assertTrue(self.deploy(self.daemon_cfg()))
        calls = [list(c) for c in self.fake.system_calls]
        tmp = calls[1][8]
        self.assertEqual(os.path.dirname(tmp), self.env.daemons_dir)
        self.assertTrue(os.path.basename(tmp).startswith(".tools-"))
        self.assertFalse(tmp.endswith(".plist"))
        self.assertFalse(os.path.exists(tmp))
        calls[1][8] = "<tmp>"
        self.assertEqual(
            calls,
            [
                ["/bin/mkdir", "-m", "755", self.env.system_log_dir],
                ["/usr/bin/install", "-S", "-m", "0644", "-o", "root", "-g", "wheel", "<tmp>",
                 path],
                [launchd.LAUNCHCTL, "bootout", "system/org.example.dns"],
                [launchd.LAUNCHCTL, "bootstrap", "system", path],
            ],
        )  # fmt: skip

        self.fake.system_calls.clear()
        self.assertTrue(self.deploy(self.cfg({}, root=True)))
        self.assertEqual(
            self.fake.system_calls,
            [[launchd.LAUNCHCTL, "bootout", "system/org.example.dns"], ["/bin/rm", "-f", path]],
        )
        self.assertFalse(os.path.exists(path))

    def test_privileged_paths_come_from_label_not_state(self):
        self.deploy(self.daemon_cfg())
        entry = self.state["launchd"]["services"]["org.example.dns"]
        entry["path"] = "/etc/passwd"
        self.assertEqual(self.diff(self.daemon_cfg()), [])
        self.fake.calls.clear()
        self.fake.system_calls.clear()

        self.assertTrue(self.deploy(self.cfg({}, root=True)))
        self.assertIn(["/bin/rm", "-f", self.daemon_path("dns")], self.fake.system_calls)
        self.assertFalse(any("/etc/passwd" in arg for c in self.fake.calls for arg in c))

    def test_tampered_state_domain_never_reaches_daemon_paths(self):
        self.deploy(self.cfg({"web": {"command": "srv"}}))
        entry = self.state["launchd"]["services"]["org.example.web"]
        entry.update(domain="system", path="/etc/passwd")
        self.fake.calls.clear()
        self.deploy(self.cfg({}, root=True))
        self.assertFalse(any("/etc/passwd" in arg for c in self.fake.calls for arg in c))
        self.assertFalse(any(c[0] == "/bin/rm" for c in self.fake.system_calls))
        self.assertTrue(os.path.exists(self.agent_path("web")))

        for label, bad in (
            ("org.example.x", {"domain": "/etc", "path": "/etc/passwd", "hash": "h"}),
            ("org.example.z", {"domain": None, "hash": "h"}),
            ("../../etc/passwd", {"domain": "system", "hash": "h"}),
            ("org.example.y", "not-a-mapping"),
        ):
            with self.subTest(label=label):
                self.state = {"launchd": {"services": {label: bad}}}
                self.fake.calls.clear()
                self.assertIn("malformed state entry", self.diff(self.cfg({}, root=True))[0])
                self.assertTrue(self.deploy(self.cfg({}, root=True)))
                self.assertEqual(self.state["launchd"]["services"], {label: bad})
                self.assertEqual(self.fake.calls, [])

    def test_malformed_state_entry_does_not_block_other_jobs(self):
        self.deploy(self.cfg({"old": {"command": "x"}}))
        self.state["launchd"]["services"]["org.example.junk"] = {"domain": None}
        self.state["launchd"]["services"]["org.example.web"] = "junk"
        raw = self.cfg({"new": {"command": "y"}, "web": {"command": "srv"}})
        changes = self.diff(raw)
        self.assertIn("  - remove user/org.example.old", changes)
        self.assertIn(f"  + create user/org.example.new -> {self.agent_path('new')}", changes)
        self.assertTrue(any("org.example.web" in c and "job held" in c for c in changes))

        with mock.patch.object(launchd, "log") as log:
            self.assertFalse(self.deploy(raw))
        self.assertTrue(any("fix or delete" in c.args[0] for c in log.call_args_list))
        services = self.state["launchd"]["services"]
        self.assertFalse(os.path.exists(self.agent_path("old")))
        self.assertNotIn("org.example.old", services)
        self.assertIn("org.example.new", self.fake.loaded)
        self.assertFalse(os.path.exists(self.agent_path("web")))
        self.assertEqual(services["org.example.junk"], {"domain": None})
        self.assertEqual(services["org.example.web"], "junk")

    def test_daemon_log_dir_created_for_every_daemon(self):
        logs = self.env.system_log_dir
        self.assertTrue(self.deploy(self.daemon_cfg()))
        self.assertIn(["/bin/mkdir", "-m", "755", logs], self.fake.system_calls)
        self.assertTrue(os.path.isdir(logs))
        self.assertFalse(any(c[0] in ("/usr/bin/touch", "/usr/sbin/chown")
                             for c in self.fake.system_calls))  # fmt: skip
        self.fake.system_calls.clear()
        self.deploy(self.daemon_cfg(command="y"))
        self.assertNotIn(["/bin/mkdir", "-m", "755", logs], self.fake.system_calls)

    def test_daemon_user_logs_prepared_like_nix(self):
        logs = self.env.system_log_dir
        files = [os.path.join(logs, "dns.log"), os.path.join(logs, "dns.error.log")]
        self.assertTrue(self.deploy(self.daemon_cfg(user="_svc")))
        log_cmds = [
            c for c in self.fake.system_calls if c[0] != launchd.LAUNCHCTL and "-S" not in c
        ]
        self.assertEqual(
            log_cmds,
            [["/bin/mkdir", "-m", "755", logs], ["/usr/bin/touch", "-h", *files],
             ["/usr/sbin/chown", "-h", "_svc:staff", *files]],
        )  # fmt: skip
        self.fake.system_calls.clear()
        self.deploy(self.daemon_cfg(user="_svc", group="wheel"))
        self.assertIn(["/usr/sbin/chown", "-h", "_svc:wheel", *files], self.fake.system_calls)

        self.tearDown()
        self.setUp()
        self.assertTrue(self.deploy(self.daemon_cfg()))
        self.assertFalse(any(c[0] in ("/usr/bin/touch", "/usr/sbin/chown")
                             for c in self.fake.system_calls))  # fmt: skip
        _s, svcs, _e = launchd.parse_launchd(self.daemon_cfg(user="-rf"))
        p = launchd.plan(_s, svcs, {}, self.env)
        self.assertTrue(any("user '-rf'" in e for e in p.errors), p.errors)

    def test_daemon_log_paths_must_be_normalized_and_inside_roots(self):
        logs = self.env.system_log_dir
        for key, value in (
            ("stdoutPath", f"{logs}/../escape.log"),
            ("stderrPath", f"{logs}/a/../../escape.log"),
            ("logDir", f"{logs}/.."),
        ):
            with self.subTest(key=key, value=value):
                _s, svcs, errors = launchd.parse_launchd(self.daemon_cfg(**{key: value}))
                self.assertEqual(svcs, [])
                self.assertIn("must not contain `..`", errors[0])

        for kwargs in (
            {"stdout_path": f"{logs}/../escape.log"},
            {"stdout_path": f"{logs}//x.log"},
            {"stderr_path": f"{logs}/./x.log"},
            {"log_dir": f"{logs}/sub/.."},
            {"stdout_path": "relative.log"},
            {"stdout_path": f"{logs}-evil/x.log"},
        ):
            with self.subTest(**kwargs):
                svc = launchd.Service(
                    name="dns", label="org.example.dns", domain="system", command="x", **kwargs
                )
                self.fake.calls.clear()
                p = launchd.plan(launchd.Settings(), [svc], {}, self.env)
                self.assertTrue(any("dns:" in e for e in p.errors), p.errors)
                self.assertEqual(p.create, [])
                self.assertFalse(launchd.apply(launchd.Settings(), p, {}, self.env))
                self.assertEqual(self.fake.system_calls, [])

    def test_retire_keeps_entry_when_reenable_fails(self):
        self.deploy(self.cfg({"web": {"command": "srv", "disabled": True}}))
        self.fake.script[("enable", "org.example.web")] = [1]
        self.assertFalse(self.deploy(self.cfg({})))
        entry = self.state["launchd"]["services"]["org.example.web"]
        self.assertEqual(entry["pending"], "enable")
        self.assertFalse(os.path.exists(self.agent_path("web")))
        self.assertIn("org.example.web", self.fake.disabled)

        self.assertEqual(self.diff(self.cfg({})), ["  - remove user/org.example.web"])
        self.assertTrue(self.deploy(self.cfg({})))
        self.assertNotIn("org.example.web", self.fake.disabled)
        self.assertEqual(self.state["launchd"]["services"], {})

    def test_logs_recorded_when_plist_write_fails(self):
        raw = self.cfg({"web": {"command": "srv"}})
        log_files = [os.path.join(self.tmp, "logs", n) for n in ("web.error.log", "web.log")]
        with mock.patch.object(launchd, "_write_plist", return_value=False):
            self.assertFalse(self.deploy(raw))
        entry = self.state["launchd"]["services"]["org.example.web"]
        self.assertEqual(
            (entry["logs"], entry["hash"], entry["pending"]), (log_files, None, "write")
        )
        self.assertFalse(os.path.exists(self.agent_path("web")))

        os.makedirs(self.env.agents_dir)
        with open(self.agent_path("web"), "wb") as f:
            plistlib.dump({"Label": "org.example.web", "Program": "/bin/other"}, f)
        self.assertIn("not managed by tools", self.diff(raw)[0])
        self.deploy(raw)
        self.assertEqual(self.read(self.agent_path("web"))["Program"], "/bin/other")

        os.remove(self.agent_path("web"))
        self.assertTrue(self.deploy(raw))
        entry = self.state["launchd"]["services"]["org.example.web"]
        self.assertNotIn("pending", entry)
        self.assertEqual(entry["logs"], log_files)

    def test_logs_kept_in_tracked_entry_when_update_write_fails(self):
        self.deploy(self.cfg({"web": {"command": "srv"}}))
        entry = self.state["launchd"]["services"]["org.example.web"]
        entry["logs"] = []
        before_hash = entry["hash"]
        with mock.patch.object(launchd, "_write_plist", return_value=False):
            self.assertFalse(self.deploy(self.cfg({"web": {"command": "srv2"}})))
        entry = self.state["launchd"]["services"]["org.example.web"]
        self.assertEqual(entry["hash"], before_hash)
        self.assertEqual(len(entry["logs"]), 2)

    def test_daemon_changes_as_user_are_left_for_root(self):
        self.deploy(self.daemon_cfg())
        before = dict(self.state["launchd"]["services"])
        with open(self.daemon_path("dns"), "rb") as f:
            plist = f.read()
        self.fake.calls.clear()
        self.fake.system_calls.clear()

        raw = self.daemon_cfg(root=False, command="dnsmasq -d")
        self.assertEqual(
            self.diff(raw), ["  ? org.example.dns: system job in this state; left alone"]
        )
        self.assertTrue(self.deploy(raw))
        self.assertTrue(self.deploy(self.cfg({}, root=False)))
        self.assertEqual(self.fake.system_calls, [])
        self.assertEqual(self.state["launchd"]["services"], before)
        with open(self.daemon_path("dns"), "rb") as f:
            self.assertEqual(f.read(), plist)

    def test_system_log_outside_allowed_roots_is_config_error(self):
        for key in ("stdoutPath", "stderrPath", "logDir"):
            with self.subTest(key):
                outside = os.path.join(self.tmp, "users", "x.log" if key != "logDir" else "d")
                raw = self.daemon_cfg(**{key: outside})
                self.assertTrue(any("must be inside" in c for c in self.diff(raw)))
                self.assertFalse(self.deploy(raw))
                self.assertEqual(self.fake.system_calls, [])
                self.assertFalse(os.path.exists(os.path.join(self.tmp, "users")))
                self.assertFalse(os.path.exists(self.daemon_path("dns")))
        self.env.system_log_roots = (os.path.join(self.tmp, "users"),)
        os.makedirs(os.path.join(self.tmp, "users"))
        raw = self.daemon_cfg(stdoutPath=os.path.join(self.tmp, "users", "x.log"))
        self.assertTrue(self.deploy(raw))

    def test_bootstrap_rc5_disabled_job_enabled_when_database_unreadable(self):
        self.fake.disabled_rc = 5
        self.fake.print_disabled_rc = 1
        self.fake.disabled.add("org.example.web")
        self.assertTrue(self.deploy(self.cfg({"web": {"command": "srv"}})))
        self.assertEqual(len(self.fake.verbs("enable")), 1)
        self.assertIn("org.example.web", self.fake.loaded)
        self.assertNotIn("pending", self.state["launchd"]["services"]["org.example.web"])

    def test_bootstrap_rc5_on_enabled_job_does_not_enable(self):
        self.fake.script[("bootstrap", "org.example.web")] = [5]
        self.assertTrue(self.deploy(self.cfg({"web": {"command": "srv"}})))
        self.assertEqual(self.fake.verbs("enable"), [])

    def test_removal_reverifies_plist_right_before_deleting(self):
        self.deploy(self.cfg({"web": {"command": "srv"}}))
        before = dict(self.state["launchd"]["services"])
        settings = launchd.Settings()
        p = launchd.plan(settings, [], self.state, self.env)
        self.assertEqual([label for label, _e in p.remove], ["org.example.web"])
        with open(self.agent_path("web"), "wb") as f:
            plistlib.dump({"Label": "org.example.web", "Program": "/edited"}, f)
        self.fake.calls.clear()

        self.assertFalse(launchd.apply(settings, p, self.state, self.env))
        self.assertTrue(os.path.exists(self.agent_path("web")))
        self.assertEqual(self.fake.verbs("bootout"), [])
        self.assertEqual(self.state["launchd"]["services"], before)


class StateRobustnessTest(LaunchdTestBase):
    def daemon_cfg(self, root=True, **extra):
        svc = {"command": "dnsmasq -k", **extra}
        return self.cfg(daemons={"dns": svc}, root=root)

    def test_non_mapping_launchd_state_is_left_alone(self):
        raw = self.cfg({"web": {"command": "srv"}})
        for bad in ("junk", ["x"], {"services": ["x"]}):
            with self.subTest(bad=bad):
                self.state = {"launchd": bad}
                self.fake.calls.clear()
                self.assertTrue(launchd.has_work({}, self.state))
                self.assertIn("not a mapping", self.diff(raw)[0])
                self.assertFalse(self.deploy(raw))
                self.assertEqual(self.state, {"launchd": bad})
                self.assertEqual(self.fake.calls, [])
                rows = launchd.status_rows(raw, self.state, self.env)
                self.assertEqual([r[0] for r in rows], ["org.example.web"])

    def test_unreadable_plist_hold_fails_with_fix_hint(self):
        self.deploy(self.daemon_cfg(secrets=True))
        real = launchd.read_plist
        path = self.daemon_path("dns")
        with (
            mock.patch.object(
                launchd, "read_plist",
                side_effect=lambda p: (True, None, False) if p == path else real(p),
            ),
            mock.patch.object(launchd, "log") as log,
        ):  # fmt: skip
            self.assertFalse(self.deploy(self.daemon_cfg(secrets=True)))
        self.assertTrue(any("check its owner" in c.args[0] for c in log.call_args_list))

    def test_never_written_entry_adopts_identical_plist(self):
        raw = self.cfg({"web": {"command": "srv"}})
        with mock.patch.object(launchd, "_write_plist", return_value=False):
            self.deploy(raw)
        self.assertEqual(self.state["launchd"]["services"]["org.example.web"]["pending"], "write")
        _s, svcs, _e = launchd.parse_launchd(raw, self.env.home)
        os.makedirs(self.env.agents_dir)
        with open(self.agent_path("web"), "wb") as f:
            plistlib.dump(launchd.desired_plist(svcs[0], _s, self.env), f)
        self.fake.loaded["org.example.web"] = self.agent_path("web")
        self.fake.calls.clear()

        self.assertEqual(self.diff(raw), ["  ~ adopt user/org.example.web (identical plist)"])
        self.assertTrue(self.deploy(raw))
        for verb in ("bootout", "bootstrap", "kickstart", "enable"):
            self.assertEqual(self.fake.verbs(verb), [], verb)
        entry = self.state["launchd"]["services"]["org.example.web"]
        self.assertNotIn("pending", entry)
        self.assertIsNotNone(entry["hash"])

    def test_never_written_entry_is_never_booted_out(self):
        foreign = {"Label": "org.example.web", "Program": "/bin/other"}
        os.makedirs(self.env.agents_dir)
        with open(self.agent_path("web"), "wb") as f:
            plistlib.dump(foreign, f)
        self.fake.loaded["org.example.web"] = self.agent_path("web")
        entry = {"domain": "user", "hash": None, "logs": [], "pending": "write"}
        self.state = {"launchd": {"services": {"org.example.web": dict(entry)}}}

        self.assertEqual(self.diff(self.cfg({})), [])
        self.assertTrue(self.deploy(self.cfg({})))
        self.assertEqual(self.fake.verbs("bootout"), [])
        self.assertEqual(self.read(self.agent_path("web")), foreign)
        self.assertEqual(self.state["launchd"]["services"], {})

        self.state = {"launchd": {"services": {"org.example.web": dict(entry)}}}
        self.fake.calls.clear()
        raw = self.cfg(daemons={"web": {"command": "srv"}}, root=True)
        self.assertTrue(self.deploy(raw))
        self.assertNotIn([launchd.LAUNCHCTL, "bootout", f"gui/{UID}/org.example.web"],
                         self.fake.calls)  # fmt: skip
        self.assertEqual(self.read(self.agent_path("web")), foreign)
        self.assertEqual(self.state["launchd"]["services"]["org.example.web"]["domain"], "system")

    def test_already_gone_disabled_job_is_reenabled_before_forgetting(self):
        self.deploy(self.cfg({"web": {"command": "srv", "disabled": True}}))
        os.remove(self.agent_path("web"))
        self.assertIn("org.example.web", self.fake.disabled)
        self.assertEqual(self.diff(self.cfg({})), ["  - remove user/org.example.web"])
        self.assertTrue(self.deploy(self.cfg({})))
        self.assertNotIn("org.example.web", self.fake.disabled)
        self.assertEqual(self.state["launchd"]["services"], {})

        self.deploy(self.cfg({"web": {"command": "srv", "disabled": True}}))
        os.remove(self.agent_path("web"))
        raw = self.cfg(daemons={"web": {"command": "srv"}}, root=True)
        self.assertTrue(self.deploy(raw))
        self.assertIn([launchd.LAUNCHCTL, "enable", f"gui/{UID}/org.example.web"], self.fake.calls)
        self.assertNotIn("org.example.web", self.fake.disabled)
        self.assertEqual(self.state["launchd"]["services"]["org.example.web"]["domain"], "system")


class ParityReviewTest(LaunchdTestBase):
    def test_agent_defaults_match_home_module(self):
        env = launchd.Env(uid=UID, home="/Users/someone", bash="/bin/bash")
        svc = launchd.Service(name="web", label="org.example.web", command="srv",
                              wait_for_secrets=True)  # fmt: skip
        settings = launchd.Settings(secrets_dir="/Users/someone/.local/state/tools/secrets")
        pl = launchd.desired_plist(svc, settings, env)
        self.assertEqual(pl["StandardOutPath"], "/tmp/agents/log/launchd/web.log")
        self.assertEqual(pl["StandardErrorPath"], "/tmp/agents/log/launchd/web.error.log")
        script = pl["ProgramArguments"][2]
        self.assertIn(
            "while [ ! -e /Users/someone/.local/state/tools/secrets/.tools-rendered ]", script
        )
        self.assertIn(
            "if [ -O /tmp/agents/log/launchd ] && [ ! -L /tmp/agents/log/launchd ]; "
            "then /bin/chmod 755",
            script,
        )

    def test_user_and_extra_user_name_conflict(self):
        for svc in (
            {"user": "_a", "extraServiceConfig": {"UserName": "_b"}},
            {"group": "g", "extraServiceConfig": {"GroupName": "h"}},
            {"stdoutPath": "/tmp/log/a.log", "extraServiceConfig": {"StandardOutPath": "/x"}},
        ):
            with self.subTest(svc):
                _s, svcs, errors = launchd.parse_launchd(
                    self.cfg(daemons={"d": {"command": "x", **svc}})
                )
                self.assertEqual(svcs, [])
                self.assertIn("not both", errors[0])
        direct = launchd.Service(name="d", label="org.example.d", domain="system", command="x",
                                 user="_a", extra_service_config={"UserName": "_b"})  # fmt: skip
        p = launchd.plan(launchd.Settings(), [direct], {}, self.env)
        self.assertTrue(any("both set" in e for e in p.errors), p.errors)

    def test_section_error_holds_every_change(self):
        logdir = os.path.join(self.tmp, "custom")
        self.deploy(self.cfg({"web": {"command": "srv"}, "old": {"command": "x"}}, logDir=logdir))
        before = self.read(self.agent_path("web"))
        self.fake.calls.clear()
        raw = self.cfg({"web": {"command": "srv2"}}, logDir=logdir, bash="relative/bash")
        _s, _svcs, errors = launchd.parse_launchd(raw)
        self.assertEqual(len(errors), 1)
        self.assertEqual(_s.log_dir, logdir)
        self.assertTrue(self.diff(raw)[0].startswith("  ! bash: 'relative/bash' must be absolute"))
        self.assertEqual(len(self.diff(raw)), 1)
        self.assertFalse(self.deploy(raw))
        self.assertEqual(self.read(self.agent_path("web")), before)
        self.assertTrue(os.path.exists(self.agent_path("old")))
        self.assertEqual(self.fake.verbs("bootout") + self.fake.verbs("bootstrap"), [])

    def test_bash_defaults_to_bin_bash_and_daemon_override_must_be_root_only(self):
        self.assertEqual(launchd.Env(uid=UID, home=self.tmp).bash, "/bin/bash")
        settings, svcs, errors = launchd.parse_launchd(
            self.cfg({"s": {"command": "x"}}, bash="/usr/local/bin/bash")
        )
        self.assertEqual(errors, [])
        pl = launchd.desired_plist(svcs[0], settings, self.env)
        self.assertEqual(pl["ProgramArguments"][0], "/usr/local/bin/bash")
        _s, _v, errors = launchd.parse_launchd(self.cfg({}, bash="bash"))
        self.assertTrue(errors)

        user_bash = os.path.join(self.tmp, "bash")
        open(user_bash, "w").close()
        self.user_owned.add(user_bash)
        raw = {**self.cfg(daemons={"d": {"command": "x"}}, root=True),
               "bash": user_bash}  # fmt: skip
        self.assertTrue(any("must be root-owned" in c for c in self.diff(raw)))
        self.assertFalse(self.deploy(raw))
        self.assertEqual(self.fake.system_calls, [])
        agent = self.cfg({"a": {"command": "x"}}, bash=user_bash)
        self.assertTrue(self.deploy(agent))

        self.user_owned.discard(user_bash)
        self.assertTrue(self.deploy(raw))
        self.writable.add(user_bash)
        self.assertTrue(any("must be root-owned" in c for c in self.diff(raw)))

    def test_daemon_bash_symlink_must_be_root_owned_and_plist_gets_target(self):
        link = os.path.join(self.tmp, "bash-link")
        os.symlink("/bin/bash", link)
        root = SimpleNamespace(st_uid=0, st_mode=0o40755)

        def lstat(path, owner=UID):
            if path == link:
                return SimpleNamespace(st_uid=owner, st_mode=0o120755)
            return root

        self.env.stat = lambda _p: root
        self.env.lstat = lstat
        raw = {**self.cfg(daemons={"d": {"command": "x"}}, root=True),
               "bash": link}  # fmt: skip
        self.assertTrue(any("must be root-owned" in c for c in self.diff(raw)))
        self.assertFalse(self.deploy(raw))
        self.assertFalse(os.path.exists(self.daemon_path("d")))

        self.env.lstat = lambda path: lstat(path, owner=0)
        self.assertTrue(self.deploy(raw))
        self.assertEqual(
            self.read(self.daemon_path("d"))["ProgramArguments"][0], os.path.realpath("/bin/bash")
        )

    def test_job_loaded_from_foreign_plist_is_never_booted_out(self):
        foreign = "/Library/LaunchAgents/org.example.web.plist"
        self.fake.loaded["org.example.web"] = foreign
        raw = self.cfg({"web": {"command": "srv"}})
        self.assertIn(f"already loaded from {foreign}", self.diff(raw)[0])
        self.assertFalse(self.deploy(raw))
        self.assertEqual(self.fake.verbs("bootout"), [])
        self.assertEqual(self.fake.loaded["org.example.web"], foreign)
        self.assertFalse(os.path.exists(self.agent_path("web")))

    def test_removal_never_boots_out_a_job_loaded_from_a_foreign_plist(self):
        self.deploy(self.cfg({"web": {"command": "srv"}}))
        os.remove(self.agent_path("web"))
        foreign = os.path.join(self.tmp, "elsewhere", "org.example.web.plist")
        self.fake.loaded["org.example.web"] = foreign
        self.fake.calls.clear()
        self.assertIn("not tools' plist", self.diff(self.cfg({}))[0])
        self.assertTrue(self.deploy(self.cfg({})))
        self.assertEqual(self.fake.verbs("bootout"), [])
        self.assertEqual(self.fake.loaded["org.example.web"], foreign)
        self.assertEqual(self.state["launchd"]["services"], {})

        del self.fake.loaded["org.example.web"]
        self.deploy(self.cfg({"web": {"command": "srv"}}))
        self.fake.loaded["org.example.web"] = foreign
        entry = self.state["launchd"]["services"]["org.example.web"]
        self.fake.calls.clear()
        self.assertFalse(launchd._retire("org.example.web", entry, {}, self.env))
        self.assertEqual(self.fake.verbs("bootout"), [])

    def test_wrapper_creates_log_dir_even_with_explicit_log_paths(self):
        logs = os.path.join(self.tmp, "logs")
        svc = {"command": "x", "stdoutPath": "/tmp/o.log", "stderrPath": "/tmp/e.log"}
        _s, svcs, _e = launchd.parse_launchd(self.cfg({"s": svc}))
        script = launchd.desired_plist(svcs[0], _s, self.env)["ProgramArguments"][2]
        self.assertIn(f"/bin/mkdir -p {logs}\n", script)

    def test_extra_service_config_log_paths_are_checked(self):
        svc = {"command": "srv", "extraServiceConfig": {"StandardOutPath": "/etc/evil"}}
        raw = self.cfg(daemons={"d": svc})
        self.assertTrue(any("/etc/evil" in c for c in self.diff(raw)))
        self.assertFalse(self.deploy(raw))
        self.assertFalse(os.path.exists(self.daemon_path("d")))
        self.assertEqual(self.fake.system_calls, [])

        ok = os.path.join(self.env.system_log_dir, "d.out")
        _s, svcs, _e = launchd.parse_launchd(
            self.cfg(daemons={"d": {"command": "srv",
                            "extraServiceConfig": {"StandardOutPath": ok}}})
        )  # fmt: skip
        self.assertEqual((svcs[0].stdout_path, svcs[0].extra_service_config), (ok, {}))

        extra = {"StandardErrorPath": "/etc/evil"}
        direct = launchd.Service(
            name="d",
            label="org.example.d",
            domain="system",
            command="x",
            extra_service_config=extra,
        )
        p = launchd.plan(launchd.Settings(), [direct], {}, self.env)
        self.assertTrue(any("/etc/evil" in e for e in p.errors), p.errors)

    def test_extra_dirs_mode_range(self):
        for mode in ("77777", "-1", "10000"):
            with self.subTest(mode):
                _s, svcs, errors = launchd.parse_launchd(
                    self.cfg({"x": {"command": "srv", "extraDirs": [{"path": "/d", "mode": mode}]}})
                )
                self.assertEqual(svcs, [])
                self.assertIn("between 0000 and 7777", errors[0])
        _s, svcs, errors = launchd.parse_launchd(
            self.cfg(
                daemons={"x": {"command": "srv", "extraDirs": [{"path": "/d", "mode": "7777"}]}}
            )
        )
        self.assertEqual(errors, [])
        self.as_user()
        direct = launchd.Service(name="x", label="org.example.x", command="srv",
                                 extra_dirs=[launchd.ExtraDir("/d", 0o77777)])  # fmt: skip
        p = launchd.plan(launchd.Settings(), [direct], {}, self.env)
        self.assertTrue(any("between 0000 and 7777" in e for e in p.errors), p.errors)


class SignOffTest(LaunchdTestBase):
    def daemon_cfg(self, **extra):
        svc = {"command": "x", **extra}
        return self.cfg(daemons={"d": svc}, root=True)

    def test_working_directory_created_before_bootstrap(self):
        wd = os.path.join(self.tmp, "work")
        created = []
        real = self.fake.run

        def run(cmd):
            if "bootstrap" in cmd:
                created.append(os.path.isdir(wd))
            return real(cmd)

        self.env.run = run
        self.assertTrue(self.deploy(self.cfg({"a": {"command": "x", "workingDirectory": wd}})))
        self.assertEqual(created, [True])

        dwd = os.path.join(self.tmp, "dwork")
        self.env.system_dir_roots = (self.tmp,)
        self.assertTrue(self.deploy(self.daemon_cfg(extraServiceConfig={"WorkingDirectory": dwd})))
        self.assertIn(["/bin/mkdir", "-m", "755", dwd], self.fake.system_calls)
        self.assertEqual(self.read(self.daemon_path("d"))["WorkingDirectory"], dwd)

        direct = launchd.Service(name="d", label="org.example.d", domain="system", command="x",
                                 working_directory="/a/../b")  # fmt: skip
        p = launchd.plan(launchd.Settings(), [direct], {}, self.env)
        self.assertTrue(any("workingDirectory" in e for e in p.errors), p.errors)

    def test_bad_state_field_types_are_malformed_entries(self):
        for field_, value in (("logs", 42), ("restartOn", {"a": 1}), ("disabled", "yes"),
                              ("hash", 5), ("pending", "bogus")):  # fmt: skip
            with self.subTest(field_):
                self.tearDown()
                self.setUp()
                self.deploy(self.cfg({"web": {"command": "srv"}}))
                self.state["launchd"]["services"]["org.example.web"][field_] = value
                raw = self.cfg({"web": {"command": "srv"}, "new": {"command": "y"}})
                self.assertFalse(self.deploy(raw))
                self.assertIn("org.example.new", self.fake.loaded)
                self.assertEqual(
                    self.state["launchd"]["services"]["org.example.web"][field_], value
                )

    def test_state_written_even_when_apply_raises(self):
        raw = self.cfg({"a": {"command": "x"}, "b": {"command": "y"}})
        real = launchd._converge

        def converge(item, *a, **kw):
            if item.svc.name == "b":
                raise RuntimeError("boom")
            return real(item, *a, **kw)

        with mock.patch.object(launchd, "_converge", side_effect=converge):
            with self.assertRaises(RuntimeError):
                self.deploy(raw)
        self.assertIn("org.example.a", self.state["launchd"]["services"])

    def test_live_disabled_override_is_corrected(self):
        raw = self.cfg({"web": {"command": "srv"}})
        self.deploy(raw)
        self.fake.disabled.add("org.example.web")
        del self.fake.loaded["org.example.web"]
        self.assertEqual(self.diff(raw), ["  ~ enable user/org.example.web"])
        self.assertTrue(self.deploy(raw))
        self.assertNotIn("org.example.web", self.fake.disabled)
        self.assertIn("org.example.web", self.fake.loaded)

        off = self.cfg({"web": {"command": "srv", "disabled": True}})
        self.deploy(off)
        self.fake.disabled.discard("org.example.web")
        self.assertEqual(self.diff(off), ["  ~ disable user/org.example.web"])
        self.assertTrue(self.deploy(off))
        self.assertIn("org.example.web", self.fake.disabled)

    def test_truncated_xml_plist_is_malformed_not_a_crash(self):
        os.makedirs(self.env.agents_dir)
        with open(self.agent_path("web"), "wb") as f:
            f.write(b'<?xml version="1.0"?><plist><dict><key>Label</key>')
        raw = self.cfg({"web": {"command": "srv"}})
        self.assertIn("malformed plist", self.diff(raw)[0])
        self.assertTrue(self.deploy(raw))
        self.assertEqual(launchd._loads(b"<plist><array/></plist>"), None)

        os.remove(self.agent_path("web"))
        self.deploy(raw)
        with open(self.agent_path("web"), "wb") as f:
            f.write(b"<?xml version")
        self.assertIn("modified since tools wrote it", self.diff(self.cfg({}))[0])
        self.assertTrue(self.deploy(self.cfg({})))
        self.assertTrue(os.path.exists(self.agent_path("web")))

    def test_daemon_log_symlinks_never_touched_or_chowned(self):
        logs = self.env.system_log_dir
        os.makedirs(logs)
        target = os.path.join(self.tmp, "victim")
        open(target, "w").close()
        os.symlink(target, os.path.join(logs, "d.log"))
        self.assertTrue(self.deploy(self.daemon_cfg(user="_svc")))
        err = os.path.join(logs, "d.error.log")
        self.assertIn(["/usr/bin/touch", "-h", err], self.fake.system_calls)
        self.assertIn(["/usr/sbin/chown", "-h", "_svc:staff", err], self.fake.system_calls)
        self.assertFalse(any(os.path.join(logs, "d.log") in c for c in self.fake.system_calls))

        self.tearDown()
        self.setUp()
        os.makedirs(os.path.join(self.tmp, "real"))
        os.symlink(os.path.join(self.tmp, "real"), self.env.system_log_dir)
        self.user_owned.add(self.env.system_log_dir)
        self.assertIn("is a symlink", " ".join(self.diff(self.daemon_cfg(user="_svc"))))
        self.assertFalse(self.deploy(self.daemon_cfg(user="_svc")))
        self.assertEqual(self.fake.system_calls, [])
        self.assertFalse(os.path.exists(self.daemon_path("d")))

    def test_user_owned_log_parent_is_refused(self):
        parent = os.path.join(self.tmp, "tmplog")
        os.makedirs(parent)
        self.user_owned.add(parent)
        logs = os.path.join(parent, "launchd")
        raw = self.daemon_cfg(logDir=logs)
        self.env.system_log_roots = (parent,)
        self.assertIn(
            f"  ! d: log dir {logs}: {parent} is not root-only "
            "(root-owned, not group/world-writable)",
            self.diff(raw),
        )
        self.assertFalse(self.deploy(raw))
        self.assertEqual(self.fake.system_calls, [])
        self.assertFalse(os.path.exists(logs))

    def test_world_writable_ancestor_is_refused(self):
        sticky = os.path.join(self.tmp, "tmp")
        os.makedirs(sticky)
        self.writable.add(sticky)
        self.env.system_log_dir = os.path.join(sticky, "log", "launchd")
        self.assertFalse(self.deploy(self.daemon_cfg()))
        self.assertFalse(os.path.exists(os.path.join(sticky, "log")))

    def test_log_dir_swapped_after_planning_is_refused_at_apply(self):
        raw = self.daemon_cfg()
        _s, svcs, _e = launchd.parse_launchd(raw, self.env.home)
        p = launchd.plan(_s, svcs, {}, self.env)
        self.assertEqual(len(p.create), 1)
        os.makedirs(os.path.join(self.tmp, "victim"))
        os.symlink(os.path.join(self.tmp, "victim"), self.env.system_log_dir)
        self.user_owned.add(self.env.system_log_dir)
        self.assertFalse(launchd.apply(_s, p, self.state, self.env))
        self.assertEqual(self.fake.system_calls, [])
        self.assertFalse(os.path.exists(self.daemon_path("d")))

    def test_mkdir_result_must_be_a_root_only_dir(self):
        logs = self.env.system_log_dir
        real = self.fake.run

        def run(cmd):
            out = real(cmd)
            if cmd[0] == "/bin/mkdir" and cmd[-1] == logs:
                self.user_owned.add(logs)
            return out

        self.env.run = run
        self.assertFalse(self.deploy(self.daemon_cfg()))
        self.assertFalse(os.path.exists(self.daemon_path("d")))

    def test_default_daemon_log_dir_is_tmp_log_like_nix(self):
        env = launchd.Env(uid=UID, home=self.tmp)
        self.assertEqual(env.system_log_dir, "/tmp/log/launchd")
        self.assertFalse(any(r.startswith(("/tmp", "/private/tmp")) for r in env.system_log_roots))

    def sticky_tmp(self):
        """A root-owned sticky world-writable dir, as /private/tmp."""
        sticky = os.path.join(self.tmp, "tmp")
        os.makedirs(sticky)
        os.chmod(sticky, 0o1755)
        self.writable.add(sticky)
        self.env.system_log_dir = os.path.join(sticky, "log", "launchd")
        return sticky

    def test_log_dir_in_sticky_tmp_is_created_root_only(self):
        sticky = self.sticky_tmp()
        logs = self.env.system_log_dir
        self.assertTrue(self.deploy(self.daemon_cfg()))
        self.assertIn(["/bin/mkdir", "-m", "755", os.path.join(sticky, "log")],
                      self.fake.system_calls)  # fmt: skip
        self.assertIn(["/bin/mkdir", "-m", "755", logs], self.fake.system_calls)
        self.assertEqual(self.read(self.daemon_path("d"))["StandardOutPath"],
                         os.path.join(logs, "d.log"))  # fmt: skip
        self.assertTrue(self.deploy(self.daemon_cfg()))

    def test_user_owned_dir_in_sticky_tmp_is_refused(self):
        sticky = self.sticky_tmp()
        os.makedirs(os.path.join(sticky, "log"))
        self.user_owned.add(os.path.join(sticky, "log"))
        self.assertIn("is not root-only", " ".join(self.diff(self.daemon_cfg())))
        self.assertFalse(self.deploy(self.daemon_cfg()))
        self.assertEqual(self.fake.system_calls, [])
        self.assertFalse(os.path.exists(self.daemon_path("d")))

    def test_root_owned_symlink_log_dir_is_refused(self):
        sticky = self.sticky_tmp()
        os.makedirs(os.path.join(sticky, "log"))
        os.makedirs(os.path.join(self.tmp, "real"))
        os.symlink(os.path.join(self.tmp, "real"), self.env.system_log_dir)
        self.assertIn(f"{self.env.system_log_dir} is a symlink",
                      " ".join(self.diff(self.daemon_cfg())))  # fmt: skip
        self.assertFalse(self.deploy(self.daemon_cfg()))
        self.assertFalse(os.path.exists(self.daemon_path("d")))

    def test_sticky_dir_is_not_allowed_as_the_log_dir_itself(self):
        sticky = self.sticky_tmp()
        self.env.system_log_dir = sticky
        self.assertFalse(self.deploy(self.daemon_cfg()))
        self.assertFalse(os.path.exists(self.daemon_path("d")))


class OpenBugsTest(LaunchdTestBase):
    def daemon_cfg(self, root=True, **extra):
        svc = {"command": "x", **extra}
        return self.cfg(daemons={"d": svc}, root=root)

    def script(self, raw):
        settings, svcs, errors = launchd.parse_launchd(raw, self.env.home)
        self.assertEqual(errors, [])
        return launchd.desired_plist(svcs[0], settings, self.env)["ProgramArguments"][2]

    def test_non_mapping_jobs_hold_every_removal(self):
        self.deploy(self.cfg({"web": {"command": "srv"}}))
        self.fake.calls.clear()
        for group in ("agents", "daemons"):
            for bad in ([], False, "", 0, ["web"]):
                with self.subTest(group=group, bad=bad):
                    raw = {**self.cfg(), group: bad}
                    self.assertTrue(launchd.has_work(raw, {}))
                    changes = self.diff(raw)
                    self.assertIn(f"  ! {group} must be a mapping of name -> job", changes)
                    self.assertFalse(any(c.startswith("  - remove") for c in changes), changes)
                    self.assertFalse(self.deploy(raw))
                    self.assertTrue(os.path.exists(self.agent_path("web")))
                    self.assertIn("org.example.web", self.state["launchd"]["services"])
        self.assertEqual(self.fake.verbs("bootout"), [])
        self.assertTrue(self.deploy(self.cfg(None)))
        self.assertFalse(os.path.exists(self.agent_path("web")))

    def test_non_mapping_section_holds_every_removal(self):
        self.deploy(self.cfg({"web": {"command": "srv"}}))
        self.fake.calls.clear()
        for bad in ([], False, "", 0):
            with self.subTest(bad=bad):
                self.assertTrue(launchd.has_work(bad, {}))
                self.assertTrue(self.diff(bad)[0].startswith("  ! launchd must be a mapping"))
                self.assertFalse(self.deploy(bad))
                self.assertTrue(os.path.exists(self.agent_path("web")))
        self.assertEqual(self.fake.verbs("bootout"), [])

    def test_cli_passes_non_mapping_section_through(self):
        state = {"launchd": {"services": {"org.example.web": {"domain": "user"}}}}
        with (
            mock.patch.object(cli, "load_json", return_value=state),
            mock.patch.object(cli, "migrate_state_file"),
            mock.patch.object(cli, "save_json"),
            mock.patch.object(cli, "state_lock"),
            mock.patch.object(cli, "install_launchd", return_value=False) as install,
        ):
            cli._deploy({"launchd": []}, "/tmp")
        install.assert_called_once()
        self.assertEqual(install.call_args.args[0], [])

    def test_extra_dir_owner_group_mode_need_a_root_daemon(self):
        entry = {"path": "/srv/d", "mode": "0750", "owner": "svc", "group": "staff"}
        rejected = {
            "agent": {"command": "x", "extraDirs": [entry]},
            "agent-mode": {"command": "x", "extraDirs": [{"path": "/srv/d", "mode": "0700"}]},
            "user": {"command": "x", "user": "_www", "extraDirs": [entry]},
            "extra-user": {
                "command": "x",
                "extraServiceConfig": {"UserName": "_www"},
                "extraDirs": [{"path": "/srv/d", "owner": "svc"}],
            },
        }
        for name, svc in rejected.items():
            with self.subTest(name):
                group = "agents" if name.startswith("agent") else "daemons"
                _s, svcs, errors = launchd.parse_launchd(self.cfg(**{group: {name: svc}}))
                self.assertEqual(svcs, [])
                self.assertIn("root daemon", errors[0])
        for user in ({}, {"user": "root"}):
            _s, svcs, errors = launchd.parse_launchd(
                self.cfg(daemons={"r": {"command": "x", "extraDirs": [entry], **user}})
            )
            self.assertEqual(errors, [])
        _s, svcs, errors = launchd.parse_launchd(
            self.cfg({"a": {"command": "x", "extraDirs": ["/srv/plain"]}})
        )
        self.assertEqual(errors, [])
        self.as_user()
        direct = launchd.Service(name="x", label="org.example.x", command="srv",
                                 extra_dirs=[launchd.ExtraDir("/d", owner="me")])  # fmt: skip
        p = launchd.plan(launchd.Settings(), [direct], {}, self.env)
        self.assertTrue(any("root daemon" in e for e in p.errors), p.errors)

    def test_wrapper_paths_expand_at_runtime_in_the_jobs_env(self):
        with mock.patch.dict(os.environ, {"HOME": "/Users/deployer", "DATA": "/deploy"}):
            script = self.script(
                self.daemon_cfg(
                    user="_www",
                    waitForPaths=["$HOME/ready"],
                    dataDir="~/data",
                    extraDirs=["${DATA}/x y"],
                )
            )
            agent = self.script(self.cfg({"a": {"command": "x", "waitForPath": "$DATA/a"}}))
        self.assertNotIn("/Users/deployer", script)
        self.assertNotIn("/deploy", script + agent)
        self.assertIn('while [ ! -e "${HOME}"/ready ]', script)
        self.assertIn('/bin/mkdir -p "${HOME}"/data', script)
        self.assertIn("/bin/mkdir -p \"${DATA}\"'/x y'", script)
        self.assertIn('while [ ! -e "${DATA}"/a ]', agent)
        for path, want in (("$HOME/ready", "/h/ready"), ("${DATA}/x y", "/d z/x y"),
                           ("/lit/$/x", "/lit/$/x"), ("/a'b/$DATA", "/a'b/d z")):  # fmt: skip
            out = subprocess.run(
                [
                    shutil.which("bash") or "/bin/bash",
                    "-c",
                    f"printf %s {launchd.shell_path(path)}",
                ],
                env={"HOME": "/h", "DATA": "d z"} if "DATA}" not in path else {"DATA": "/d z"},
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertEqual(out, want)
        for bad in ("$(id)/x", "relative/$HOME", "`id`/x", "$1/x"):
            with self.subTest(bad):
                _s, svcs, errors = launchd.parse_launchd(
                    self.cfg({"a": {"command": "x", "waitForPaths": [bad]}})
                )
                self.assertEqual(svcs, [], bad)

    def test_deploy_time_paths_with_vars_rejected_for_daemons_of_another_user(self):
        login = pwd.getpwuid(os.getuid()).pw_name
        fields = {
            "workingDirectory": "~/w",
            "stdoutPath": "$HOME/x.log",
            "logDir": "${TMPDIR}/logs",
            "restartOn": ["~/conf"],
            "keepAlive": {"pathState": {"$HOME/flag": True}},
            "extraServiceConfig": {"WorkingDirectory": "$HOME/w"},
        }
        for account in ({"user": "_www"}, {}, {"extraServiceConfig": {"UserName": "_www"}}):
            for key, value in fields.items():
                with self.subTest(account=account, key=key):
                    svc = {"command": "x", key: value}
                    if "extraServiceConfig" in account:
                        svc["extraServiceConfig"] = {
                            **svc.get("extraServiceConfig", {}),
                            **account["extraServiceConfig"],
                        }
                    else:
                        svc.update(account)
                    _s, svcs, errors = launchd.parse_launchd(
                        self.cfg(daemons={"d": svc}), "/Users/me"
                    )
                    self.assertEqual(svcs, [])
                    self.assertIn("expanded at deploy", errors[0])
        _s, svcs, errors = launchd.parse_launchd(
            self.cfg(
                daemons={"d": {"command": "x", "user": "_www"}}, logDir="$HOME/logs"
            ),  # fmt: skip
            "/Users/me",
        )
        self.assertTrue(errors and "expanded at deploy" in errors[0], errors)
        _s, svcs, errors = launchd.parse_launchd(
            self.cfg(
                daemons={
                    "d": {
                        "command": "x",
                        "user": login,
                        "workingDirectory": "~/w",
                    }
                }
            ),
            "/Users/me",
        )
        self.assertEqual(errors, [])
        self.assertEqual(svcs[0].working_directory, "/Users/me/w")

    def test_failed_workdir_mkdir_fails_the_job_before_its_plist(self):
        allowed = os.path.join(self.tmp, "varlib")
        os.makedirs(allowed)
        os.makedirs(self.env.system_log_dir)
        self.env.system_dir_roots = (allowed,)
        wd = os.path.join(allowed, "svc", "w")
        raw = self.daemon_cfg(workingDirectory=wd)
        self.assertEqual(
            self.diff(raw), [f"  + create system/org.example.d -> {self.daemon_path('d')}"]
        )
        self.fake.fail_tools.add("/bin/mkdir")
        self.assertFalse(self.deploy(raw))
        self.assertFalse(os.path.exists(self.daemon_path("d")))
        self.assertEqual(self.fake.verbs("bootout") + self.fake.verbs("bootstrap"), [])
        self.assertNotIn("hash", self.state["launchd"]["services"].get("org.example.d", {}) or {})

        self.fake.fail_tools.clear()
        self.assertTrue(self.deploy(self.daemon_cfg(workingDirectory=wd, disabled=True)))

    def test_workdir_under_a_user_owned_root_is_blocked_in_plan(self):
        allowed = os.path.join(self.tmp, "homebrew-var")
        os.makedirs(allowed)
        self.user_owned.add(allowed)
        self.env.system_dir_roots = (allowed,)
        wd = os.path.join(allowed, "svc")
        changes = self.diff(self.daemon_cfg(workingDirectory=wd))
        self.assertEqual(len(changes), 1)
        self.assertTrue(changes[0].startswith("  ! skip org.example.d: workingDirectory"), changes)
        self.assertIn(f"{allowed} is not root-only", changes[0])
        self.deploy(self.daemon_cfg(workingDirectory=wd))
        self.assertFalse(os.path.exists(wd))
        self.assertFalse(os.path.exists(self.daemon_path("d")))

    def test_homebrew_var_is_not_a_default_workdir_root(self):
        roots = launchd.Env(uid=UID, home=self.tmp).system_dir_roots
        self.assertNotIn("/opt/homebrew/var", roots)
        self.assertNotIn("/usr/local/var", roots)

    def test_daemon_working_directory_created_only_under_allowed_roots(self):
        allowed = os.path.join(self.tmp, "varlib")
        os.makedirs(allowed)
        self.env.system_dir_roots = (allowed,)
        outside = os.path.join(self.tmp, "elsewhere", "w")
        changes = self.diff(self.daemon_cfg(workingDirectory=outside))
        self.assertTrue(any("workingDirectory" in c and c.startswith("  ! skip") for c in changes))
        self.deploy(self.daemon_cfg(workingDirectory=outside))
        self.assertFalse(any(c[0] == "/bin/mkdir" and outside in c for c in self.fake.system_calls))
        self.assertFalse(os.path.exists(outside))
        self.assertFalse(os.path.exists(self.daemon_path("d")))

        os.makedirs(os.path.join(self.tmp, "target"))
        os.symlink(os.path.join(self.tmp, "target"), os.path.join(allowed, "link"))
        via_link = os.path.join(allowed, "link", "w")
        self.deploy(self.daemon_cfg(workingDirectory=via_link))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "target", "w")))

        inside = os.path.join(allowed, "svc", "w")
        self.assertTrue(self.deploy(self.daemon_cfg(workingDirectory=inside)))
        self.assertIn(["/bin/mkdir", "-m", "755", inside], self.fake.system_calls)

        existing = os.path.join(self.tmp, "exists")
        os.makedirs(existing)
        self.assertTrue(self.deploy(self.daemon_cfg(workingDirectory=existing)))

        self.env.system_dir_roots = (os.path.join(self.tmp, "missing-root"),)
        gone = os.path.join(self.tmp, "missing-root", "w")
        self.assertTrue(any(c.startswith("  ! skip") for c in self.diff(
            self.daemon_cfg(workingDirectory=gone))))  # fmt: skip

    def test_disabling_daemon_with_missing_workdir_still_unloads(self):
        allowed = os.path.join(self.tmp, "varlib")
        os.makedirs(allowed)
        self.env.system_dir_roots = (allowed,)
        wd = os.path.join(self.tmp, "elsewhere")
        os.makedirs(wd)
        self.assertTrue(self.deploy(self.daemon_cfg(workingDirectory=wd)))
        self.assertIn("org.example.d", self.fake.loaded)
        os.rmdir(wd)
        raw = self.daemon_cfg(workingDirectory=wd, disabled=True)
        self.assertEqual(self.diff(raw), ["  ~ disable system/org.example.d"])
        self.assertTrue(self.deploy(raw))
        self.assertNotIn("org.example.d", self.fake.loaded)
        self.assertIn("org.example.d", self.fake.disabled)
        self.assertFalse(any(c[0] == "/bin/mkdir" and wd in c for c in self.fake.system_calls))

    def test_status_rows_keep_both_domains_during_a_move(self):
        self.deploy(self.cfg({"dns": {"command": "dnsmasq -k"}}))
        raw = self.cfg(daemons={"dns": {"command": "dnsmasq -k"}})
        rows = launchd.status_rows(raw, self.state, self.env)
        self.assertEqual(
            [(label, domain, st.loaded) for label, domain, st in rows],
            [("org.example.dns", "system", False), ("org.example.dns", "user", True)],
        )

    def test_diff_shows_removals_held_by_errors(self):
        self.deploy(self.cfg({"web": {"command": "srv"}, "old": {"command": "x"}}))
        raw = self.cfg({"web": {"command": "srv"}, "broken": {"command": "x", "bogus": True}})
        changes = self.diff(raw)
        self.assertIn("  ! held: remove user/org.example.old (config has errors)", changes)
        self.assertFalse(self.deploy(raw))
        self.assertTrue(os.path.exists(self.agent_path("old")))

    def test_diff_shows_adoption(self):
        raw = self.cfg({"web": {"command": "srv"}})
        self.deploy(raw)
        self.state = {}
        self.assertEqual(self.diff(raw), ["  ~ adopt user/org.example.web (identical plist)"])


class DisabledLogDirTest(LaunchdTestBase):
    LABEL = "org.example.d"

    def daemon_cfg(self, **extra):
        return self.cfg(daemons={"d": {"command": "x", **extra}})

    def running_daemon_with_user_owned_logs(self):
        self.assertTrue(self.deploy(self.daemon_cfg()))
        self.assertIn(self.LABEL, self.fake.loaded)
        self.user_owned.add(self.env.system_log_dir)
        self.fake.calls.clear()
        self.fake.system_calls.clear()

    def no_writes(self):
        writes = [c for c in self.fake.system_calls if c[0] != launchd.LAUNCHCTL]
        self.assertEqual(writes, [])

    def test_disabled_daemon_is_stopped_although_its_log_dir_is_not_root_only(self):
        self.running_daemon_with_user_owned_logs()
        before = self.state["launchd"]["services"][self.LABEL]["hash"]
        raw = self.daemon_cfg(disabled=True)
        changes = self.diff(raw)
        self.assertIn("not root-only", " ".join(changes))
        self.assertIn(f"  ~ disable system/{self.LABEL} (bootout only; nothing written)", changes)
        self.assertTrue(self.deploy(raw))
        self.assertNotIn(self.LABEL, self.fake.loaded)
        self.assertIn(self.LABEL, self.fake.disabled)
        self.no_writes()
        entry = self.state["launchd"]["services"][self.LABEL]
        self.assertEqual((entry["hash"], entry["disabled"]), (before, True))
        self.assertEqual(self.diff(raw), [c for c in changes if not c.startswith("  ~")])
        self.assertTrue(self.deploy(raw))

    def test_stopped_disabled_daemon_with_bad_log_dir_does_not_hold_removals(self):
        self.assertTrue(
            self.deploy(self.cfg(daemons={"d": {"command": "x"}, "old": {"command": "z"}}))
        )
        self.user_owned.add(self.env.system_log_dir)
        raw = self.daemon_cfg(disabled=True)
        self.assertTrue(self.deploy(raw))
        self.assertNotIn("org.example.old", self.fake.loaded)
        self.assertFalse(os.path.exists(self.daemon_path("old")))
        self.assertNotIn("org.example.old", self.state["launchd"]["services"])
        changes = self.diff(raw)
        self.assertFalse([c for c in changes if c.startswith(("  ! held", "  ~", "  -"))], changes)
        self.assertTrue(self.deploy(raw))

    def test_disabled_daemon_needing_a_rewrite_is_stopped_without_writing(self):
        self.running_daemon_with_user_owned_logs()
        with open(self.daemon_path("d"), "rb") as f:
            old = f.read()
        self.assertTrue(self.deploy(self.daemon_cfg(command="y", disabled=True)))
        self.assertNotIn(self.LABEL, self.fake.loaded)
        self.assertIn(self.LABEL, self.fake.disabled)
        self.no_writes()
        with open(self.daemon_path("d"), "rb") as f:
            self.assertEqual(f.read(), old)

    def test_log_dir_swapped_after_planning_still_lets_disable_proceed(self):
        self.assertTrue(self.deploy(self.daemon_cfg()))
        settings, svcs, _e = launchd.parse_launchd(self.daemon_cfg(disabled=True), self.env.home)
        p = launchd.plan(settings, svcs, self.state, self.env)
        self.assertEqual(len(p.disable), 1)
        self.user_owned.add(self.env.system_log_dir)
        self.fake.system_calls.clear()
        self.assertTrue(launchd.apply(settings, p, self.state, self.env))
        self.assertNotIn(self.LABEL, self.fake.loaded)
        self.assertIn(self.LABEL, self.fake.disabled)
        self.no_writes()

    def test_update_stopped_for_a_log_dir_swapped_after_planning_says_stopped(self):
        self.assertTrue(self.deploy(self.daemon_cfg()))
        raw = self.daemon_cfg(command="y", disabled=True)
        settings, svcs, _e = launchd.parse_launchd(raw, self.env.home)
        p = launchd.plan(settings, svcs, self.state, self.env)
        self.assertEqual(len(p.update), 1)
        self.user_owned.add(self.env.system_log_dir)
        with mock.patch.object(launchd, "log") as log:
            self.assertTrue(launchd.apply(settings, p, self.state, self.env))
        notes = [c.args[0] for c in log.call_args_list]
        self.assertIn(f"Stopped launchd job: {self.LABEL} (log dir not root-only)", notes)
        self.assertFalse([n for n in notes if n.startswith("Updated launchd job")], notes)
        self.assertNotIn(self.LABEL, self.fake.loaded)
        self.assertIn(self.LABEL, self.fake.disabled)

    def foreign_daemon(self, plist):
        os.makedirs(self.env.daemons_dir, exist_ok=True)
        with open(self.daemon_path("d"), "wb") as f:
            plistlib.dump(plist, f)
        self.fake.loaded[self.LABEL] = self.daemon_path("d")
        os.makedirs(self.env.system_log_dir)
        self.user_owned.add(self.env.system_log_dir)

    def changing_launchctl(self):
        return [c for c in self.fake.calls if c[0] == launchd.LAUNCHCTL and c[1] != "print"
                and c[1] != "print-disabled"]  # fmt: skip

    def test_foreign_plist_is_never_disabled_or_booted_out(self):
        self.foreign_daemon({"Label": self.LABEL, "ProgramArguments": ["/other"]})
        raw = self.daemon_cfg(disabled=True)
        self.assertIn(
            f"  ! skip {self.LABEL}: {self.daemon_path('d')}: exists and is not managed by tools",
            self.diff(raw),
        )
        self.assertTrue(self.deploy(raw))
        self.assertEqual(self.changing_launchctl(), [])
        self.assertEqual(self.fake.system_calls, [])
        self.assertIn(self.LABEL, self.fake.loaded)
        self.assertNotIn(self.LABEL, self.fake.disabled)
        self.assertNotIn(self.LABEL, (self.state.get("launchd") or {}).get("services") or {})

    def test_identical_untracked_plist_is_stopped(self):
        raw = self.daemon_cfg(disabled=True)
        settings, svcs, _e = launchd.parse_launchd(raw, self.env.home)
        self.foreign_daemon(launchd.desired_plist(svcs[0], settings, self.env))
        self.assertTrue(self.deploy(raw))
        self.assertNotIn(self.LABEL, self.fake.loaded)
        self.assertIn(self.LABEL, self.fake.disabled)
        self.no_writes()
        entry = self.state["launchd"]["services"][self.LABEL]
        self.assertEqual(entry["disabled"], True)
        self.assertIsNotNone(entry["hash"])
        self.assertTrue(self.deploy(self.cfg(daemons={})))
        self.assertNotIn(self.LABEL, self.fake.disabled)
        self.assertFalse(os.path.exists(self.daemon_path("d")))
        self.assertEqual(self.state["launchd"]["services"], {})

    def test_identical_plist_swapped_before_the_stop_is_left_alone(self):
        raw = self.daemon_cfg(disabled=True)
        settings, svcs, _e = launchd.parse_launchd(raw, self.env.home)
        self.foreign_daemon(launchd.desired_plist(svcs[0], settings, self.env))
        p = launchd.plan(settings, svcs, self.state, self.env)
        self.assertEqual(len(p.stop), 1)
        with open(self.daemon_path("d"), "wb") as f:
            plistlib.dump({"Label": self.LABEL, "ProgramArguments": ["/other"]}, f)
        self.fake.calls.clear()
        self.assertFalse(launchd.apply(settings, p, self.state, self.env))
        self.assertEqual(self.changing_launchctl(), [])
        self.assertIn(self.LABEL, self.fake.loaded)
        self.assertNotIn(self.LABEL, self.state["launchd"]["services"])

    def test_plist_swapped_for_a_foreign_one_after_planning_is_not_stopped(self):
        raw = self.daemon_cfg(disabled=True)
        settings, svcs, _e = launchd.parse_launchd(raw, self.env.home)
        self.foreign_daemon(launchd.desired_plist(svcs[0], settings, self.env))
        self.user_owned.discard(self.env.system_log_dir)
        p = launchd.plan(settings, svcs, self.state, self.env)
        self.assertEqual(len(p.disable), 1)
        with open(self.daemon_path("d"), "wb") as f:
            plistlib.dump({"Label": self.LABEL, "ProgramArguments": ["/other"]}, f)
        self.user_owned.add(self.env.system_log_dir)
        self.fake.calls.clear()
        self.assertFalse(launchd.apply(settings, p, self.state, self.env))
        self.assertEqual(self.changing_launchctl(), [])
        self.assertIn(self.LABEL, self.fake.loaded)

    def test_enabled_daemon_with_a_non_root_only_log_dir_is_still_held(self):
        self.running_daemon_with_user_owned_logs()
        self.assertFalse(self.deploy(self.daemon_cfg(command="y")))
        self.assertEqual(self.fake.system_calls, [])
        self.assertIn(self.LABEL, self.fake.loaded)

    def test_disabled_daemon_never_written_is_not_created(self):
        os.makedirs(self.env.system_log_dir)
        self.cfg(daemons={})
        self.user_owned.add(self.env.system_log_dir)
        self.assertTrue(self.deploy(self.daemon_cfg(disabled=True)))
        self.assertEqual(self.fake.system_calls, [])
        self.assertFalse(os.path.exists(self.daemon_path("d")))


class MissingProgramTest(LaunchdTestBase):
    """A job whose program is missing (its package failed to install) is not started."""

    def test_missing_program_is_not_started_until_it_exists(self):
        program = os.path.join(self.tmp, "bin", "tailscaled")
        raw = self.cfg(daemons={"ts": {"command": program, "keepAlive": False}})
        self.assertIn(f"  ! skip org.example.ts: program {program} not found; not starting it",
                      self.diff(raw))  # fmt: skip
        self.deploy(raw)
        self.assertFalse(os.path.exists(self.daemon_path("ts")))
        self.assertEqual(self.fake.verbs("bootstrap"), [])

        os.makedirs(os.path.dirname(program))
        open(program, "w").close()
        self.assertTrue(self.deploy(raw))
        self.assertTrue(os.path.exists(self.daemon_path("ts")))
        self.assertIn("org.example.ts", self.fake.loaded)

    def test_program_arguments_and_quoted_commands(self):
        app = "/Applications/No Such.app/Contents/MacOS/app"
        args = launchd.Service("a", "l", program_arguments=[app, "-v"])
        quoted = launchd.Service("a", "l", command=f'"{app}" \\\n  --flag')
        self.assertEqual(launchd.program_of(args), app)
        self.assertEqual(launchd.program_of(quoted), app)
        self.assertIsNone(launchd.program_of(launchd.Service("a", "l", command="srv --x")))
        for svc in (
            launchd.Service("a", "l", command="/opt/${X}/bin/foo"),
            launchd.Service("a", "l", command="/opt/`uname -m`/foo"),
            launchd.Service("a", "l", command="/opt/x/foo", pre_start="install foo"),
        ):
            self.assertIsNone(launchd.program_of(svc), svc)

    def test_not_judged_when_waiting_or_relative_or_disabled(self):
        missing = os.path.join(self.tmp, "Volumes", "Storage", "bin", "x")
        cases = {
            "relative command": {"command": "srv --flag"},
            "waitForPaths absent": {
                "command": "/nonexistent/bin/x",
                "waitForPaths": [os.path.join(self.tmp, "absent")],
            },
            "disabled": {"command": "/nonexistent/bin/x", "disabled": True},
        }
        for name, svc in cases.items():
            with self.subTest(name):
                self.assertNotIn("not found", "\n".join(self.diff(self.cfg({"a": svc}))))
        self.env.ismount = lambda _p: False
        self.assertNotIn("not found", "\n".join(self.diff(self.cfg({"a": {"command": missing}}))))


class CliGatingTest(unittest.TestCase):
    def _deploy(self, config, state):
        with (
            mock.patch.object(cli, "load_json", return_value=state),
            mock.patch.object(cli, "migrate_state_file"),
            mock.patch.object(cli, "save_json"),
            mock.patch.object(cli, "state_lock"),
            mock.patch.object(cli, "install_launchd", return_value=True) as install,
        ):
            cli._deploy(config, "/tmp")
        return install

    def test_skipped_when_absent(self):
        self._deploy({}, {}).assert_not_called()

    def test_runs_when_only_state_remains(self):
        state = {"launchd": {"services": {"org.example.web": {"domain": "user"}}}}
        self._deploy({}, state).assert_called_once()

    def test_runs_when_configured(self):
        self._deploy({"launchd": {"agents": {"x": {"command": "y"}}}}, {}).assert_called_once()


class CliStatusTest(unittest.TestCase):
    def test_status_is_read_only_and_lists_rows(self):
        rows = [
            ("org.example.a", "user", launchd.Status(True, "running", 42, "0")),
            ("org.example.b", "system", launchd.Status(False)),
        ]
        with (
            mock.patch.object(cli, "_load_merged_config", return_value={}),
            mock.patch.object(cli, "load_json", return_value={}),
            mock.patch.object(cli, "save_json") as save,
            mock.patch.object(cli, "state_lock"),
            mock.patch.object(cli, "status_rows", return_value=rows),
        ):
            result = CliRunner().invoke(cli.main, ["status"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("user/org.example.a: running pid=42 last exit=0", result.output)
        self.assertIn("system/org.example.b: not loaded", result.output)
        save.assert_not_called()

    def test_status_as_root_reads_only_the_root_records(self):
        user = {"version": 2, "launchd": {"services": {"u": {}}}}
        root = {"launchd": {"services": {"r": {}}}}
        with (
            mock.patch.object(cli, "_invoker", return_value=SimpleNamespace(root=True)),
            mock.patch.object(cli, "_load_merged_config", return_value={"stateFile": "/u/s.json"}),
            mock.patch("tools.state.os.path.isfile", return_value=True),
            mock.patch.object(cli, "load_json", return_value={**user, "root": root}) as load,
            mock.patch.object(cli, "status_rows", return_value=[]) as rows,
        ):
            result = CliRunner().invoke(cli.main, ["status"])
        self.assertEqual(result.exit_code, 0, result.output)
        load.assert_called_once_with("/u/s.json")
        self.assertEqual(rows.call_args.args[1], root)


if __name__ == "__main__":
    unittest.main()
