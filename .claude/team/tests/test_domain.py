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
      -f) [[ -n "${FAKE_NO_ANCHOR:-}" ]] || grep -v '^#' "$4" | sed 's/port 80 ->/port = 80 ->/' > "$d/anchor" ;;
      -F) rm -f "$d/anchor" ;;
      -s) cat "$d/anchor" 2>/dev/null || true ;;
    esac ;;
esac
"""
FAKE_TOOLS = {
    "lsof": """#!/bin/bash
echo "lsof $*" >> "$FAKE_LOG"
port=""
for a in "$@"; do [[ "$a" == -iTCP:* ]] && port="${a#-iTCP:}"; done
f="$FAKE_DIR/listen.$port"
[[ -s "$f" ]] || exit 1
if [[ " $* " == *" -t "* ]]; then cat "$f"; else while read -r pid; do echo "Python $pid me 3u IPv4 TCP 127.0.0.1:$port (LISTEN)"; done < "$f"; fi
""",
    "nc": '#!/bin/bash\necho "nc $*" >> "$FAKE_LOG"\n[[ -z "${FAKE_DIRECT_FAIL:-}" ]]\n',
    "curl": '#!/bin/bash\necho "curl $*" >> "$FAKE_LOG"\n[[ -n "${FAKE_CURL_FAIL:-}" ]] && exit 7\nprintf "HTTP/1.0 200 OK\\r\\nServer: team-dashboard\\r\\n\\r\\n"\n',
}
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

    def install(self, hosts_line, port=7777, addr="127.0.0.1", comment=""):
        (self.etc / "hosts").write_text(HOSTS + hosts_line)
        (self.etc / "pf.anchors" / "claude-team").write_text(
            f"{comment}rdr pass on lo0 inet proto tcp from any to {addr} port 80 -> 127.0.0.1 port {port}\n")


class UrlTest(Base):
    def test_fallback_without_domain(self):
        self.assertIsNone(T.dashboard_host())
        self.assertEqual(T.dashboard_url(), "http://127.0.0.1:7777")

    def test_installed(self):
        self.install("127.0.0.77 mission.control # claude-team\n", addr="127.0.0.77")
        self.assertEqual(T.dashboard_url(), "http://mission.control")

    def test_localhost_name(self):
        self.install("127.0.0.1 team.localhost # claude-team\n")
        self.assertEqual(T.dashboard_url(), "http://team.localhost")

    def test_trailing_whitespace_on_marker(self):
        self.install("127.0.0.77 mission.control # claude-team \t\r\n", addr="127.0.0.77")
        self.assertEqual(T.dashboard_url(), "http://mission.control")

    def test_anchor_ownership(self):
        line = "127.0.0.77 mission.control # claude-team\n"
        cases = [("# claude-team port=7777 backend=7778\n", 7778, 7778), ("# claude-team port=7777 backend=7801\n", 7801, 7801),
                 ("", 7777, 7777), ("", 7778, None), ("# claude-team port=7780 backend=7781\n", 7781, None),
                 ("# claude-team port=7777 backend=7778\n", 7790, None), ("", 99999, None)]
        for comment, target, backend in cases:
            self.install(line, port=target, addr="127.0.0.77", comment=comment)
            self.assertEqual(T.anchor_backend(), backend, (comment, target))
            self.assertEqual(T.dashboard_host(), "mission.control" if backend else None, (comment, target))

    def test_port_mismatch_falls_back(self):
        self.install("127.0.0.77 mission.control # claude-team\n", port=7790, addr="127.0.0.77")
        self.assertEqual(T.dashboard_url(), "http://127.0.0.1:7777")

    def test_unmarked_line_ignored(self):
        self.install("127.0.0.77 mission.control\n127.0.0.77 x.test # claude-team-old\n", addr="127.0.0.77")
        self.assertEqual(T.dashboard_url(), "http://127.0.0.1:7777")

    def test_hosts_line_without_anchor(self):
        (self.etc / "hosts").write_text(HOSTS + "127.0.0.77 mission.control # claude-team\n")
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
        for name, body in (("pfctl", FAKE_PFCTL), ("ifconfig", FAKE_IFCONFIG), *FAKE_TOOLS.items()):
            (self.fake / name).write_text(body)
            (self.fake / name).chmod(0o755)
        self.log = self.base / "calls.log"
        self.state = self.etc / "db" / "claude-team"
        self.listen(7777, 4242)

    def listen(self, port, *pids):
        f = self.fake / f"listen.{port}"
        if pids:
            f.write_text("".join(f"{p}\n" for p in pids))
        elif f.exists():
            f.unlink()

    def nothing_left(self):
        self.assertFalse((self.etc / "LaunchDaemons" / "com.claude-team.domain.plist").exists())
        self.assertFalse((self.etc / "pf.anchors" / "claude-team").exists())
        self.assertFalse(self.state.exists())
        self.assertNotIn("claude-team", (self.etc / "hosts").read_text())
        self.assertEqual(self.fake_state("anchor"), "")
        self.assertEqual(self.aliases(), [])
        self.assertIn(self.fake_state("refs"), ("", "0"))
        self.assertEqual(self.fake_state("tokens"), "")
        self.assertEqual(self.url(), "http://127.0.0.1:7777")

    def env(self, **kw):
        e = dict(os.environ, TEAM_ETC=str(self.etc), TEAM_PORT="7777", TEAM_PFCTL=str(self.fake / "pfctl"),
                 TEAM_IFCONFIG=str(self.fake / "ifconfig"), TEAM_LSOF=str(self.fake / "lsof"), TEAM_NC=str(self.fake / "nc"),
                 TEAM_CURL=str(self.fake / "curl"), FAKE_LOG=str(self.log), FAKE_DIR=str(self.fake))
        for k in ("TEAM_HOST", "TEAM_ADDR", "TEAM_DAEMONS", "TEAM_BACKEND_PORT", "FAKE_DIRECT_FAIL", "FAKE_CURL_FAIL", "FAKE_E_FAIL", "FAKE_NO_ANCHOR"):
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
        self.assertIn("host mission.control, address 127.0.0.77, port 80 -> 127.0.0.1:7778", out)
        self.assertIn("ok: pf anchor com.apple/claude-team redirects 127.0.0.77:80 to 127.0.0.1:7778", out)
        self.assertIn("ok: 127.0.0.1:7777 still answers direct connections", out)
        self.assertIn("ok: http://mission.control/ answers with the dashboard", out)
        self.assertIn("ok: pf is enabled", out)
        first = (self.etc / "hosts").read_bytes()
        self.assertTrue(first.endswith(b"127.0.0.77 mission.control # claude-team\n"))
        self.assertIn("to 127.0.0.77 port 80 -> 127.0.0.1 port 7778", (self.etc / "pf.anchors" / "claude-team").read_text())
        self.assertEqual(self.aliases(), ["127.0.0.77"])
        self.assertEqual((self.state / "alias").read_text().strip(), "127.0.0.77")
        self.assertEqual((self.state / "pf.token").read_text().strip(), "10001")
        self.assertEqual((self.state / "pf.prior").read_text().strip(), "disabled")
        self.assertEqual(self.url(), "http://mission.control")
        self.assertEqual(self.url(TEAM_PORT="7790"), "http://127.0.0.1:7790")
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
        self.assertIn("to 127.0.0.1 port 80 -> 127.0.0.1 port 7778", (self.etc / "pf.anchors" / "claude-team").read_text())
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
        r = subprocess.run(["bash", str(INSTALL)], capture_output=True, text=True, env=self.env(FAKE_E_FAIL="1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("boot.sh failed (the previous pf token was kept)", r.stderr)
        self.assertIn("DIOCSTARTREF", r.stderr)
        self.assertEqual((self.state / "pf.token").read_text().strip(), "10001")
        self.assertNotIn("pfctl -X 10001", self.calls())
        self.assertFalse((self.state / "pf.token.tmp").exists())

    def test_failing_enable_on_a_fresh_install_undoes_it(self):
        r = self.run_fail(INSTALL, FAKE_E_FAIL="1")
        self.assertIn("FAIL: pf is not enabled", r.stderr)
        self.assertIn("FAIL: the redirect is not in place", r.stderr)
        self.assertNotIn("previous install", r.stderr)
        self.nothing_left()

    def test_redirect_missing_fails(self):
        r = self.run_fail(INSTALL, FAKE_NO_ANCHOR="1")
        self.assertIn("FAIL: pf anchor com.apple/claude-team has no rdr rule for 127.0.0.77:80", r.stderr)
        self.assertIn("the redirect is not in place", r.stderr)
        self.nothing_left()

    def test_failed_upgrade_does_not_leave_the_new_name(self):
        self.run_script(INSTALL, TEAM_HOST="team.test")
        (self.fake / "anchor").write_text("rdr pass on lo0 inet proto tcp from any to 127.0.0.77 port = 80 -> 127.0.0.1 port 7777\n")
        r = self.run_fail(INSTALL, FAKE_NO_ANCHOR="1")
        self.assertIn("The previous install (http://team.test) is removed as well: the local domain is now uninstalled.", r.stderr)
        self.assertNotIn("mission.control", (self.etc / "hosts").read_text())
        self.nothing_left()

    def test_pf_conf_and_listener_checks(self):
        self.listen(7777)
        r = subprocess.run(["bash", str(INSTALL)], capture_output=True, text=True, env=self.env())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('WARNING: ' + str(self.etc / "pf.conf") + ' has no rdr-anchor "com.apple/*" line', r.stderr)
        self.assertIn("WARNING: nothing listened on 127.0.0.1:7777 before the install", r.stderr)
        self.assertIn("installed: http://mission.control (unchecked)", r.stdout)
        self.assertFalse(any(c.startswith(("nc ", "curl ")) for c in self.calls()))
        (self.etc / "pf.conf").write_text('scrub-anchor "com.apple/*"\nrdr-anchor "com.apple/*"\nanchor "com.apple/*"\n')
        self.listen(7777, 4242)
        out = self.run_script(INSTALL)
        self.assertIn('evaluates rdr-anchor "com.apple/*"', out)
        self.assertIn("nc -z -G 2 -w 2 127.0.0.1 7777", self.calls())
        self.assertIn("curl -s -I --max-time 2 http://mission.control/", self.calls())

    def test_direct_connections_failing_after_the_redirect_undo_everything(self):
        original = (self.etc / "hosts").read_bytes()
        r = self.run_fail(INSTALL, FAKE_DIRECT_FAIL="1")
        self.assertIn("FAIL: after loading the redirect, TCP connections to 127.0.0.1:7777 time out", r.stderr)
        self.assertIn("would run: launchctl bootout system/com.claude-team.domain", r.stderr)
        self.assertIn("undone: no LaunchDaemon, anchor file, pf rules, pf enable reference, alias we added, hosts line or", r.stderr)
        self.assertIn("The dashboard stays at http://127.0.0.1:7777.", r.stderr)
        self.assertIn("diag-domain.sh", r.stderr)
        self.assertNotIn("previous install", r.stderr)
        c = self.calls()
        self.assertLess(c.index("nc -z -G 2 -w 2 127.0.0.1 7777"), c.index("pfctl -X 10001"))
        self.assertLess(c.index("pfctl -X 10001"), c.index("pfctl -a com.apple/claude-team -F all"))
        self.assertNotIn("installed:", r.stdout)
        self.nothing_left()
        self.assertEqual((self.etc / "hosts").read_bytes(), original)

    def test_direct_failure_on_an_upgrade_says_the_domain_is_gone(self):
        self.run_script(INSTALL, TEAM_HOST="team.test")
        legacy = "rdr pass on lo0 inet proto tcp from any to 127.0.0.77 port 80 -> 127.0.0.1 port 7777\n"
        (self.etc / "pf.anchors" / "claude-team").write_text(legacy)
        r = self.run_fail(INSTALL, FAKE_DIRECT_FAIL="1")
        self.assertIn("The previous install (http://team.test) is removed as well: the local domain is now uninstalled.", r.stderr)
        self.nothing_left()

    def test_backend_port_held_by_another_program_is_refused(self):
        original = (self.etc / "hosts").read_bytes()
        self.listen(7778, 5151)
        r = self.run_fail(INSTALL)
        self.assertIn("127.0.0.1:7778 is already in use by another program", r.stderr)
        self.assertIn("Python 5151 me 3u IPv4 TCP 127.0.0.1:7778 (LISTEN)", r.stderr)
        self.assertIn("TEAM_BACKEND_PORT=<port>", r.stderr)
        self.assertFalse(self.state.exists())
        self.assertEqual((self.etc / "hosts").read_bytes(), original)
        self.assertFalse(any(c.startswith(("pfctl", "ifconfig")) for c in self.calls()))
        self.listen(7778, 4242)
        self.run_script(INSTALL)
        self.listen(7778, 4242, 5151)
        self.run_fail(INSTALL)

    def test_domain_not_answering_fails_without_flushing(self):
        r = self.run_fail(INSTALL, FAKE_CURL_FAIL="1")
        self.assertIn("FAIL: http://mission.control/ did not answer with the dashboard", r.stderr)
        self.assertIn("The redirect stays installed.", r.stderr)
        self.assertIn("team.py dashboard stop && python3 ", r.stderr)
        self.assertNotIn("pfctl -a com.apple/claude-team -F all", self.calls())
        self.assertIn("-> 127.0.0.1 port 7778", self.fake_state("anchor"))

    def test_reinstall_over_a_legacy_anchor_converges(self):
        (self.etc / "hosts").write_text(HOSTS + "127.0.0.77 mission.control # claude-team\n")
        (self.etc / "pf.anchors" / "claude-team").write_text("rdr pass on lo0 inet proto tcp from any to 127.0.0.77 port 80 -> 127.0.0.1 port 7777\n")
        (self.fake / "anchor").write_text("rdr pass on lo0 inet proto tcp from any to 127.0.0.77 port = 80 -> 127.0.0.1 port 7777\n")
        self.run_script(INSTALL)
        self.assertIn("-> 127.0.0.1 port 7778", (self.etc / "pf.anchors" / "claude-team").read_text())
        self.assertIn("-> 127.0.0.1 port 7778", self.fake_state("anchor"))
        self.assertEqual((self.etc / "hosts").read_text().count("# claude-team"), 1)

    def test_upgrade_from_the_old_name_and_direct_anchor(self):
        original = (self.etc / "hosts").read_bytes()
        self.run_script(INSTALL, TEAM_HOST="team.test")
        legacy = "rdr pass on lo0 inet proto tcp from any to 127.0.0.77 port 80 -> 127.0.0.1 port 7777\n"
        (self.etc / "pf.anchors" / "claude-team").write_text(legacy)
        (self.fake / "anchor").write_text(legacy.replace("port 80 ->", "port = 80 ->"))
        self.assertEqual(self.url(), "http://team.test")
        self.log.unlink()
        out = self.run_script(INSTALL)
        hosts = (self.etc / "hosts").read_text()
        self.assertEqual([l for l in hosts.splitlines() if "claude-team" in l], ["127.0.0.77 mission.control # claude-team"])
        self.assertNotIn("team.test", hosts)
        self.assertEqual((self.etc / "pf.anchors" / "claude-team").read_text(),
                         "# claude-team port=7777 backend=7778\nrdr pass on lo0 inet proto tcp from any to 127.0.0.77 port 80 -> 127.0.0.1 port 7778\n")
        self.assertIn("-> 127.0.0.1 port 7778", self.fake_state("anchor"))
        self.assertEqual(self.aliases(), ["127.0.0.77"])
        self.assertEqual((self.state / "alias").read_text().strip(), "127.0.0.77")
        c = self.calls()
        self.assertNotIn("ifconfig lo0 -alias 127.0.0.77", c)
        self.assertLess(c.index("pfctl -E"), c.index("pfctl -X 10001"))
        self.assertEqual((self.state / "pf.token").read_text().strip(), "10002")
        self.assertEqual(self.fake_state("refs"), "1")
        self.assertIn("installed: http://mission.control", out)
        self.assertEqual(self.url(), "http://mission.control")
        self.run_script(UNINSTALL)
        self.assertEqual((self.etc / "hosts").read_bytes(), original)

    def test_diag_writes_only_a_new_regular_file(self):
        body = (ROOT / "schedule" / "diag-domain.sh").read_text()
        body = body.replace("if (( EUID != 0 )); then", "if false; then").replace("PATH=/usr/bin:/bin:/usr/sbin:/sbin", 'PATH="$FAKEBIN:/usr/bin:/bin:/usr/sbin:/sbin"')
        (self.base / "diag.sh").write_text(body)
        for name in ("pfctl", "ifconfig", "lsof", "nc", "curl", "chown"):
            (self.fake / name).write_text(f'#!/bin/bash\necho "{name} $*"\n')
            (self.fake / name).chmod(0o755)
        target = self.base / "target"
        (self.base / "link").symlink_to(target)
        (self.base / "exists").write_text("keep\n")
        env = dict(os.environ, FAKEBIN=str(self.fake), SUDO_UID="501", SUDO_GID="20")
        for name in ("link", "exists"):
            r = subprocess.run(["bash", str(self.base / "diag.sh"), str(self.base / name)], capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 1, name)
            self.assertIn("as a new regular file", r.stderr)
        self.assertFalse(target.exists())
        self.assertEqual((self.base / "exists").read_text(), "keep\n")
        out = self.base / "diag.txt"
        r = subprocess.run(["bash", str(self.base / "diag.sh"), str(out)], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        text = out.read_text()
        self.assertIn("===== pfctl -a com.apple/claude-team -s nat\npfctl -a com.apple/claude-team -s nat", text)
        self.assertIn("direct connections time out by design", text)
        self.assertIn(f"chown 501:20 {out}", r.stdout)
        self.assertEqual(oct(out.stat().st_mode & 0o777), "0o644")
        self.assertEqual(subprocess.run(["bash", str(self.base / "diag.sh")], capture_output=True, env=env).returncode, 2)

    def test_backend_port_validation(self):
        for kw in ({"TEAM_BACKEND_PORT": "7777"}, {"TEAM_BACKEND_PORT": "80"}, {"TEAM_BACKEND_PORT": "077"}, {"TEAM_PORT": "7790", "TEAM_BACKEND_PORT": "7790"}):
            r = subprocess.run(["bash", str(INSTALL)], capture_output=True, text=True, env=self.env(**kw))
            self.assertEqual(r.returncode, 1, kw)
            self.assertIn("TEAM_BACKEND_PORT must be", r.stderr)
        self.run_script(INSTALL, TEAM_BACKEND_PORT="7801")
        self.assertIn("-> 127.0.0.1 port 7801", (self.etc / "pf.anchors" / "claude-team").read_text())

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
        for k in ("TEAM_PFCTL", "TEAM_IFCONFIG", "TEAM_LSOF", "TEAM_NC", "TEAM_CURL"):
            env.pop(k)
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
        self.assertEqual(self.url(), "http://mission.control")
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
        self.assertEqual(once, b"127.0.0.1 localhost\n::1 localhost\n127.0.0.77 mission.control # claude-team\n")

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
        (self.etc / "hosts").write_text(HOSTS + "127.0.0.77 mission.control # claude-team  \t\n")
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
                   {"TEAM_ADDR": "127.0.0.0"}, {"TEAM_ADDR": "127.255.255.255"}, {"TEAM_HOST": "mission.control", "TEAM_ADDR": "127.0.0.1"}):
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

        def status(host, to=port):
            c = http.client.HTTPConnection("127.0.0.1", to, timeout=5)
            c.request("GET", "/api/snapshot", headers={"Host": host})
            code = c.getresponse().status
            c.close()
            return code
        try:
            for _ in range(50):
                if T.port_open(port):
                    break
                time.sleep(0.1)
            self.assertEqual(status("mission.control"), 403)
            self.assertFalse(T.port_open(port + 1))
            self.run_script(INSTALL, TEAM_PORT=str(port))
            self.assertEqual({h: status(h) for h in ("mission.control", "mission.control:80", f"127.0.0.1:{port}", "evil.example")},
                             {"mission.control": 200, "mission.control:80": 200, f"127.0.0.1:{port}": 200, "evil.example": 403})
            for _ in range(50):
                if T.port_open(port + 1):
                    break
                time.sleep(0.1)
            self.assertEqual(status("mission.control", port + 1), 200)
            self.assertEqual(status("evil.example", port + 1), 403)
            self.run_script(UNINSTALL)
            self.assertEqual(status("mission.control"), 403)
            self.assertEqual(status(f"127.0.0.1:{port}"), 200)
            for _ in range(50):
                if not T.port_open(port + 1):
                    break
                time.sleep(0.1)
            self.assertFalse(T.port_open(port + 1))
            custom = free_port()
            self.run_script(INSTALL, TEAM_PORT=str(port), TEAM_BACKEND_PORT=str(custom))
            for _ in range(50):
                if T.port_open(custom):
                    break
                time.sleep(0.1)
            self.assertEqual(status("mission.control", custom), 200)
            rule = (self.etc / "pf.anchors" / "claude-team").read_text()
            (self.etc / "pf.anchors" / "claude-team").write_text(rule.replace(f"port={port} ", f"port={port + 50} "))
            for _ in range(50):
                if not T.port_open(custom):
                    break
                time.sleep(0.1)
            self.assertFalse(T.port_open(custom))
            self.assertEqual(status("mission.control"), 403)
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
