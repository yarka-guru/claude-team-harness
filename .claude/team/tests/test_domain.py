import http.client
import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import teamlib as T

HOSTS = "127.0.0.1 localhost\n255.255.255.255 broadcasthost\n::1 localhost\n"
INSTALL = ROOT / "schedule" / "install-domain.sh"
UNINSTALL = ROOT / "schedule" / "uninstall-domain.sh"
FAKE_PFCTL = """#!/bin/bash
echo "pfctl $*" >> "$FAKE_LOG"
d="$FAKE_DIR"
refs=$(cat "$d/refs" 2>/dev/null || echo 0)
case "$1" in
  -E)
    if [[ -n "${FAKE_E_FAIL:-}" ]]; then echo "pfctl: DIOCSTARTREF: Operation not permitted" >&2; exit 1; fi
    n=$(( $(cat "$d/n" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$d/n"
    echo "1000$n" >> "$d/tokens"
    echo $(( refs + 1 )) > "$d/refs"
    touch "$d/enabled"
    printf 'No ALTQ support in kernel\\npf enabled\\nToken : 1000%s\\n' "$n" >&2 ;;
  -X)
    grep -qx "$2" "$d/tokens" 2>/dev/null || { echo "pfctl: invalid token" >&2; exit 1; }
    grep -vx "$2" "$d/tokens" > "$d/t" || true
    mv "$d/t" "$d/tokens"
    echo $(( refs - 1 )) > "$d/refs"
    (( refs - 1 > 0 )) || rm -f "$d/enabled" ;;
  -e) touch "$d/enabled" ;;
  -s) if [[ -f "$d/enabled" ]]; then echo "Status: Enabled for 0 days 00:00:01"; else echo "Status: Disabled"; fi ;;
  -a)
    case "$3" in
      -f) [[ -n "${FAKE_NO_ANCHOR:-}" ]] || sed 's/port 80 ->/port = 80 ->/' "$4" > "$d/anchor" ;;
      -F) rm -f "$d/anchor" ;;
      -s) cat "$d/anchor" 2>/dev/null || true ;;
    esac ;;
esac
"""
FAKE_IFCONFIG = """#!/bin/bash
echo "ifconfig $*" >> "$FAKE_LOG"
f="$FAKE_DIR/aliases"
touch "$f"
if [[ $# -eq 1 ]]; then
  printf 'lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384\\n\\tinet 127.0.0.1 netmask 0xff000000\\n'
  while read -r a; do printf '\\tinet %s netmask 0xff000000\\n' "$a"; done < "$f"
  exit 0
fi
if [[ "$2" == alias ]]; then grep -qx "$3" "$f" || echo "$3" >> "$f"; fi
if [[ "$2" == -alias ]]; then grep -vx "$3" "$f" > "$f.t" || true; mv "$f.t" "$f"; fi
"""


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    try:
        return s.getsockname()[1]
    finally:
        s.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.etc = self.base / "etc"
        (self.etc / "pf.anchors").mkdir(parents=True)
        (self.etc / "hosts").write_text(HOSTS)
        self.patches = [mock.patch.object(T, "ETC", self.etc), mock.patch.object(T, "PORT", 7777)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def install(self, hosts_line, port=7777, addr="127.0.0.1"):
        (self.etc / "hosts").write_text(HOSTS + hosts_line)
        (self.etc / "pf.anchors" / "claude-team").write_text(
            f"rdr pass on lo0 inet proto tcp from any to {addr} port 80 -> 127.0.0.1 port {port}\n")


class UrlTest(Base):
    def test_fallback_without_domain(self):
        self.assertIsNone(T.dashboard_host())
        self.assertEqual(T.dashboard_url(), "http://127.0.0.1:7777")

    def test_installed(self):
        self.install("127.0.0.77 team.test # claude-team\n", addr="127.0.0.77")
        self.assertEqual(T.dashboard_url(), "http://team.test")

    def test_localhost_name(self):
        self.install("127.0.0.1 team.localhost # claude-team\n")
        self.assertEqual(T.dashboard_url(), "http://team.localhost")

    def test_trailing_whitespace_on_marker(self):
        self.install("127.0.0.77 team.test # claude-team \t\r\n", addr="127.0.0.77")
        self.assertEqual(T.dashboard_url(), "http://team.test")

    def test_port_mismatch_falls_back(self):
        self.install("127.0.0.77 team.test # claude-team\n", port=7778, addr="127.0.0.77")
        self.assertEqual(T.dashboard_url(), "http://127.0.0.1:7777")

    def test_unmarked_line_ignored(self):
        self.install("127.0.0.77 team.test\n127.0.0.77 x.test # claude-team-old\n", addr="127.0.0.77")
        self.assertEqual(T.dashboard_url(), "http://127.0.0.1:7777")

    def test_hosts_line_without_anchor(self):
        (self.etc / "hosts").write_text(HOSTS + "127.0.0.77 team.test # claude-team\n")
        self.assertEqual(T.dashboard_url(), "http://127.0.0.1:7777")

    def test_unreadable_files(self):
        (self.etc / "hosts").unlink()
        self.assertEqual(T.dashboard_url(), "http://127.0.0.1:7777")


@unittest.skipUnless(platform.system() == "Darwin", "the domain scripts are macOS only")
class ScriptTest(Base):
    def setUp(self):
        super().setUp()
        self.fake = self.base / "fake"
        self.fake.mkdir()
        for name, body in (("pfctl", FAKE_PFCTL), ("ifconfig", FAKE_IFCONFIG)):
            (self.fake / name).write_text(body)
            (self.fake / name).chmod(0o755)
        self.log = self.base / "calls.log"
        self.state = self.etc / "db" / "claude-team"

    def env(self, **kw):
        e = dict(os.environ, TEAM_ETC=str(self.etc), TEAM_PORT="7777", TEAM_PFCTL=str(self.fake / "pfctl"),
                 TEAM_IFCONFIG=str(self.fake / "ifconfig"), FAKE_LOG=str(self.log), FAKE_DIR=str(self.fake))
        for k in ("TEAM_HOST", "TEAM_ADDR", "TEAM_DAEMONS"):
            e.pop(k, None)
        e.update(kw)
        return e

    def run_script(self, script, **kw):
        r = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=self.env(**kw))
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def aliases(self):
        f = self.fake / "aliases"
        return f.read_text().split() if f.exists() else []

    def url(self, **kw):
        code = "import teamlib; print(teamlib.dashboard_url())"
        return subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, env=self.env(**kw)).stdout.strip()

    def run_fail(self, script, **kw):
        r = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=self.env(**kw))
        self.assertNotEqual(r.returncode, 0, r.stdout)
        return r

    def fake_state(self, name):
        f = self.fake / name
        return f.read_text().strip() if f.exists() else ""

    def test_round_trip_tokens_and_alias(self):
        original = (self.etc / "hosts").read_bytes()
        out = self.run_script(INSTALL)
        self.assertIn("host team.test, address 127.0.0.77, port 80 -> 127.0.0.1:7777", out)
        self.assertIn("ok: pf anchor com.apple/claude-team redirects 127.0.0.77:80 to 127.0.0.1:7777", out)
        self.assertIn("ok: pf is enabled", out)
        first = (self.etc / "hosts").read_bytes()
        self.assertTrue(first.endswith(b"127.0.0.77 team.test # claude-team\n"))
        self.assertIn("to 127.0.0.77 port 80 -> 127.0.0.1 port 7777", (self.etc / "pf.anchors" / "claude-team").read_text())
        self.assertEqual(self.aliases(), ["127.0.0.77"])
        self.assertEqual((self.state / "alias").read_text().strip(), "127.0.0.77")
        self.assertEqual((self.state / "pf.token").read_text().strip(), "10001")
        self.assertEqual((self.state / "pf.prior").read_text().strip(), "disabled")
        self.assertEqual(self.url(), "http://team.test")
        self.assertEqual(self.url(TEAM_PORT="7778"), "http://127.0.0.1:7778")
        self.log.unlink()
        self.run_script(INSTALL)
        self.assertEqual((self.etc / "hosts").read_bytes(), first)
        c = self.calls()
        self.assertLess(c.index("pfctl -E"), c.index("pfctl -X 10001"))
        self.assertEqual(sum(1 for x in c if x == "pfctl -E"), 1)
        self.assertEqual((self.state / "pf.token").read_text().strip(), "10002")
        self.assertEqual(self.fake_state("refs"), "1")
        self.assertEqual(self.aliases(), ["127.0.0.77"])
        self.log.unlink()
        self.run_script(INSTALL, TEAM_HOST="team.localhost")
        self.assertIn("ifconfig lo0 -alias 127.0.0.77", self.calls())
        self.assertEqual(self.aliases(), [])
        self.assertFalse((self.state / "alias").exists())
        self.assertTrue((self.etc / "hosts").read_bytes().endswith(b"127.0.0.1 team.localhost # claude-team\n"))
        self.assertIn("to 127.0.0.1 port 80 -> 127.0.0.1 port 7777", (self.etc / "pf.anchors" / "claude-team").read_text())
        self.assertEqual(self.url(), "http://team.localhost")
        self.run_script(INSTALL)
        self.log.unlink()
        self.run_script(UNINSTALL)
        c = self.calls()
        self.assertIn("pfctl -X 10004", c)
        self.assertIn("pfctl -a com.apple/claude-team -F all", c)
        self.assertIn("ifconfig lo0 -alias 127.0.0.77", c)
        self.assertNotIn("pfctl -E", c)
        self.assertNotIn("pfctl -e", c)
        self.assertEqual(self.fake_state("refs"), "0")
        self.assertFalse((self.fake / "enabled").exists())
        self.assertEqual(self.aliases(), [])
        self.assertEqual((self.etc / "hosts").read_bytes(), original)
        self.assertEqual((self.etc / "hosts.claude-team.bak").read_bytes(), original)
        self.assertFalse(self.state.exists())
        self.assertFalse((self.etc / "pf.anchors" / "claude-team").exists())
        self.assertFalse((self.etc / "LaunchDaemons" / "com.claude-team.domain.plist").exists())
        self.assertEqual(self.url(), "http://127.0.0.1:7777")

    def test_failing_enable_keeps_the_old_token(self):
        self.run_script(INSTALL)
        self.log.unlink()
        r = self.run_fail(INSTALL, FAKE_E_FAIL="1")
        self.assertIn("boot.sh failed; the previous pf token was kept", r.stderr)
        self.assertIn("DIOCSTARTREF", r.stderr)
        self.assertEqual((self.state / "pf.token").read_text().strip(), "10001")
        self.assertNotIn("pfctl -X 10001", self.calls())
        self.assertFalse((self.state / "pf.token.tmp").exists())

    def test_redirect_missing_fails(self):
        r = self.run_fail(INSTALL, FAKE_NO_ANCHOR="1")
        self.assertIn("FAIL: pf anchor com.apple/claude-team has no rdr rule for 127.0.0.77:80", r.stderr)
        self.assertIn("the redirect is not in place", r.stderr)

    def test_pf_conf_and_listener_checks(self):
        port = free_port()
        r = subprocess.run(["bash", str(INSTALL)], capture_output=True, text=True, env=self.env(TEAM_PORT=str(port)))
        self.assertIn('WARNING: ' + str(self.etc / "pf.conf") + ' has no rdr-anchor "com.apple/*" line', r.stderr)
        self.assertIn(f"WARNING: nothing listens on 127.0.0.1:{port}", r.stderr)
        (self.etc / "pf.conf").write_text('scrub-anchor "com.apple/*"\nrdr-anchor "com.apple/*"\nanchor "com.apple/*"\n')
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        try:
            out = self.run_script(INSTALL, TEAM_PORT=str(srv.getsockname()[1]))
        finally:
            srv.close()
        self.assertIn('evaluates rdr-anchor "com.apple/*"', out)
        self.assertIn("ok: something listens on 127.0.0.1:", out)

    def test_prior_enabled_pf_is_turned_back_on(self):
        (self.fake / "enabled").touch()
        self.run_script(INSTALL)
        self.assertEqual((self.state / "pf.prior").read_text().strip(), "enabled")
        self.log.unlink()
        self.run_script(UNINSTALL)
        c = self.calls()
        self.assertLess(c.index("pfctl -X 10001"), c.index("pfctl -e"))
        self.assertTrue((self.fake / "enabled").exists())

    def test_dry_run_without_fakes_calls_nothing(self):
        env = self.env()
        env.pop("TEAM_PFCTL")
        env.pop("TEAM_IFCONFIG")
        r = subprocess.run(["bash", str(INSTALL)], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("would run: ifconfig lo0 alias 127.0.0.77 up", r.stderr)
        self.assertIn("boot.sh not run, pf checks skipped", r.stdout)
        self.assertFalse(self.log.exists())
        self.assertFalse((self.state / "pf.token").exists())
        self.assertIn("/sbin/pfctl", (self.state / "boot.sh").read_text())
        env["TEAM_PFCTL"] = str(self.fake / "pfctl")
        r = subprocess.run(["bash", str(INSTALL)], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("pfctl -E", self.calls())
        r = subprocess.run(["bash", str(UNINSTALL)], capture_output=True, text=True, env=env | {"TEAM_PFCTL": ""})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("would run: pfctl -a com.apple/claude-team -F all", r.stderr)
        self.assertIn("would run: ifconfig lo0 -alias 127.0.0.77", r.stderr)
        self.assertEqual((self.etc / "hosts").read_text(), HOSTS)

    def test_symlink_loop_refused(self):
        (self.etc / "hosts").unlink()
        (self.etc / "hosts").symlink_to("hosts2")
        (self.etc / "hosts2").symlink_to("hosts")
        r = self.run_fail(INSTALL)
        self.assertIn("more than 16 symlink hops", r.stderr)
        self.assertFalse((self.etc / "pf.anchors" / "claude-team").exists())

    def test_etc_path_with_space_and_ampersand(self):
        etc = self.base / "etc D&x"
        etc.mkdir()
        (etc / "hosts").write_text(HOSTS)
        self.run_script(INSTALL, TEAM_ETC=str(etc))
        state = etc / "db" / "claude-team"
        plist = etc / "LaunchDaemons" / "com.claude-team.domain.plist"
        self.assertEqual(subprocess.run(["plutil", "-lint", str(plist)], capture_output=True).returncode, 0)
        args = subprocess.run(["plutil", "-extract", "ProgramArguments", "json", "-o", "-", str(plist)], capture_output=True, text=True).stdout
        self.assertEqual(json.loads(args), ["/bin/bash", str(state / "boot.sh")])
        log = subprocess.run(["plutil", "-extract", "StandardErrorPath", "raw", str(plist)], capture_output=True, text=True).stdout.strip()
        self.assertEqual(log, str(state / "boot.log"))
        self.assertEqual((state / "pf.token").read_text().strip(), "10001")
        self.assertIn(f"pfctl -a com.apple/claude-team -f {etc}/pf.anchors/claude-team", self.calls())
        self.run_script(UNINSTALL, TEAM_ETC=str(etc))
        self.assertEqual((etc / "hosts").read_text(), HOSTS)
        self.assertFalse(state.exists())

    def round_trip(self):
        hosts = self.etc / "hosts"
        target = hosts.resolve()
        original = target.read_bytes()
        self.run_script(INSTALL)
        once = target.read_bytes()
        self.assertEqual(once.count(b"# claude-team"), 1)
        self.run_script(INSTALL)
        self.assertEqual(target.read_bytes(), once)
        self.assertEqual(self.url(), "http://team.test")
        self.run_script(UNINSTALL)
        self.assertEqual(target.read_bytes(), original)
        return once

    def test_crlf_hosts(self):
        (self.etc / "hosts").write_bytes(b"127.0.0.1 localhost\r\n::1 localhost\r\n")
        once = self.round_trip()
        self.assertTrue(once.startswith(b"127.0.0.1 localhost\r\n::1 localhost\r\n"))

    def test_hosts_without_trailing_newline(self):
        (self.etc / "hosts").write_bytes(b"127.0.0.1 localhost\n::1 localhost")
        once = self.round_trip()
        self.assertEqual(once, b"127.0.0.1 localhost\n::1 localhost\n127.0.0.77 team.test # claude-team\n")

    def test_symlinked_hosts(self):
        real = self.base / "private" / "hosts"
        real.parent.mkdir()
        real.write_text(HOSTS)
        (self.etc / "hosts").unlink()
        (self.etc / "hosts").symlink_to(Path("..") / "private" / "hosts")
        self.round_trip()
        self.assertTrue((self.etc / "hosts").is_symlink())
        self.assertEqual(real.read_text(), HOSTS)

    def test_marker_with_trailing_whitespace_replaced(self):
        (self.etc / "hosts").write_text(HOSTS + "127.0.0.77 team.test # claude-team  \t\n")
        self.run_script(INSTALL)
        self.assertEqual((self.etc / "hosts").read_text().count("# claude-team"), 1)
        self.run_script(UNINSTALL)
        self.assertEqual((self.etc / "hosts").read_text(), HOSTS)

    def test_alias_we_did_not_create_is_kept(self):
        (self.fake / "aliases").write_text("127.0.0.77\n")
        self.run_script(INSTALL)
        self.assertNotIn("ifconfig lo0 alias 127.0.0.77 up", self.calls())
        self.assertFalse((self.state / "alias").exists())
        self.assertNotIn("lo0 alias", (self.state / "boot.sh").read_text())
        self.run_script(UNINSTALL)
        self.assertFalse(any("-alias" in x for x in self.calls()))
        self.assertEqual(self.aliases(), ["127.0.0.77"])

    def test_daemon_defaults_under_temp_etc(self):
        out = self.run_script(INSTALL)
        plist = self.etc / "LaunchDaemons" / "com.claude-team.domain.plist"
        self.assertTrue(plist.exists())
        self.assertNotIn("/Library/LaunchDaemons", out)
        self.assertEqual(subprocess.run(["plutil", "-lint", str(plist)], capture_output=True).returncode, 0)
        args = subprocess.run(["plutil", "-extract", "ProgramArguments", "json", "-o", "-", str(plist)], capture_output=True, text=True).stdout
        self.assertEqual(json.loads(args), ["/bin/bash", str(self.state / "boot.sh")])
        boot = (self.state / "boot.sh").read_text()
        self.assertLess(boot.index('"$pfctl" -E'), boot.index('"$pfctl" -X'))
        self.assertLess(boot.index("lo0 alias 127.0.0.77 up"), boot.index("-a com.apple/claude-team -f"))

    def test_bad_input_refused(self):
        for kw in ({"TEAM_HOST": "evil host"}, {"TEAM_HOST": "Team.test"}, {"TEAM_PORT": "80"}, {"TEAM_PORT": "01024"},
                   {"TEAM_PORT": "99999"}, {"TEAM_PORT": "7777x"}, {"TEAM_ADDR": "10.0.0.1"}, {"TEAM_ADDR": "127.0.0.256"},
                   {"TEAM_ADDR": "127.1.2"}, {"TEAM_ADDR": "127.0.0.077x"}, {"TEAM_ADDR": "127.000.0.1"}, {"TEAM_ADDR": "127.0.0.077"},
                   {"TEAM_ADDR": "127.0.0.0"}, {"TEAM_ADDR": "127.255.255.255"}, {"TEAM_HOST": "team.test", "TEAM_ADDR": "127.0.0.1"}):
            r = subprocess.run(["bash", str(INSTALL)], capture_output=True, text=True, env=self.env(**kw))
            self.assertEqual(r.returncode, 1, kw)
        self.assertEqual((self.etc / "hosts").read_text(), HOSTS)
        self.assertFalse(self.state.exists())
        self.assertFalse((self.etc / "pf.anchors" / "claude-team").exists())
        self.assertEqual(self.calls(), [])

    def test_server_follows_install_without_restart(self):
        port = free_port()
        env = self.env(TEAM_PORT=str(port), TEAM_DB=str(self.base / "team.db"), TEAM_RITUALS=str(self.base / "rituals"))
        p = subprocess.Popen([sys.executable, str(ROOT / "dashboard" / "server.py")], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def status(host):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", "/api/snapshot", headers={"Host": host})
            code = c.getresponse().status
            c.close()
            return code
        try:
            for _ in range(50):
                if T.port_open(port):
                    break
                time.sleep(0.1)
            self.assertEqual(status("team.test"), 403)
            self.run_script(INSTALL, TEAM_PORT=str(port))
            self.assertEqual({h: status(h) for h in ("team.test", "team.test:80", f"127.0.0.1:{port}", "evil.example")},
                             {"team.test": 200, "team.test:80": 200, f"127.0.0.1:{port}": 200, "evil.example": 403})
            self.run_script(UNINSTALL)
            self.assertEqual(status("team.test"), 403)
            self.assertEqual(status(f"127.0.0.1:{port}"), 200)
        finally:
            p.terminate()
            p.wait(5)



@unittest.skipUnless(platform.system() == "Darwin", "launchd jobs are macOS only")
class RitualInstallTest(unittest.TestCase):
    def test_plist_values_are_escaped(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            sched = base / "ws R&D <1>" / ".claude" / "team" / "schedule"
            sched.mkdir(parents=True)
            for name in ("install.sh", "run-ritual.sh"):
                (sched / name).write_bytes((ROOT / "schedule" / name).read_bytes())
            (base / "bin").mkdir()
            (base / "bin" / "launchctl").write_text("#!/bin/bash\necho \"launchctl $*\" >> \"$FAKE_LOG\"\n"
                                                    "[[ $1 == list ]] && echo '-\t0\tcom.claude-team.daily'\nexit 0\n")
            (base / "bin" / "launchctl").chmod(0o755)
            env = dict(os.environ, HOME=str(base / "home"), PATH=f"{base / 'bin'}:{os.environ['PATH']}", FAKE_LOG=str(base / "l.log"),
                       CLAUDE_BIN="/opt/a&b <x>/claude", TEAM_LANG="uk", RITUAL_MODEL="opus")
            r = subprocess.run(["bash", str(sched / "install.sh")], capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            plist = base / "home" / "Library" / "LaunchAgents" / "com.claude-team.retro.plist"
            self.assertEqual(subprocess.run(["plutil", "-lint", str(plist)], capture_output=True).returncode, 0)
            get = lambda key: json.loads(subprocess.run(["plutil", "-extract", key, "json", "-o", "-", str(plist)], capture_output=True, text=True).stdout)
            self.assertEqual(get("ProgramArguments")[0], str(sched / "run-ritual.sh"))
            self.assertEqual(get("EnvironmentVariables"), {"CLAUDE_BIN": "/opt/a&b <x>/claude", "TEAM_LANG": "uk", "RITUAL_MODEL": "opus"})
            r = subprocess.run(["bash", str(sched / "install.sh")], capture_output=True, text=True, env=dict(env, RITUAL_MODEL="opus;x"))
            self.assertEqual(r.returncode, 1)
            self.assertIn("RITUAL_MODEL may contain only", r.stderr)


if __name__ == "__main__":
    unittest.main()
