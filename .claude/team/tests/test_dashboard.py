import argparse
import http.server
import io
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import teamlib as T
import team
sys.path.insert(0, str(ROOT / "dashboard"))
import server as S

LISTENER = "import socket, sys, time\ns = socket.socket()\ns.bind(('127.0.0.1', int(sys.argv[1])))\ns.listen(5)\ntime.sleep(30)\n"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    try:
        return s.getsockname()[1]
    finally:
        s.close()


class EnsureDashboardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "team"
        (self.root / "dashboard").mkdir(parents=True)
        self.state = Path(self.tmp.name) / "state"
        self.port = free_port()
        self.procs = []
        self.patches = [mock.patch.object(T, "ROOT", self.root), mock.patch.object(T, "STATE", self.state), mock.patch.object(T, "PORT", self.port)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        for p in self.procs:
            p.kill()
            p.wait()
        self.tmp.cleanup()

    def server(self, body):
        (self.root / "dashboard" / "server.py").write_text(body)

    def spawn(self, script, port):
        p = subprocess.Popen([sys.executable, str(script), str(port)])
        self.procs.append(p)
        for _ in range(50):
            if T.port_open(port):
                return p
            time.sleep(0.1)
        self.fail("listener did not start")

    def foreign(self, port):
        other = Path(self.tmp.name) / "other" / "team" / "dashboard" / "server.py"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_text(LISTENER)
        return self.spawn(other, port)

    def dashboard(self, args):
        return team.cmd_dashboard(None, argparse.Namespace(action=args))

    def pid(self):
        f = self.state / "dashboard.pid"
        return f.read_text().strip() if f.exists() else None

    def test_child_that_dies_on_bind_keeps_the_old_pid(self):
        self.state.mkdir()
        (self.state / "dashboard.pid").write_text("999999")
        self.server("import sys\nsys.exit('OSError: [Errno 48] Address already in use')\n")
        self.assertFalse(T.ensure_dashboard())
        self.assertEqual(self.pid(), "999999")

    def test_live_server_process_blocks_a_second_spawn(self):
        self.server("import time\ntime.sleep(30)\n")
        self.assertTrue(T.ensure_dashboard())
        first = self.pid()
        self.assertFalse(T.port_open())
        self.assertTrue(T.server_alive())
        self.assertFalse(T.ensure_dashboard())
        self.assertEqual(self.pid(), first)
        os.kill(int(first), 9)
        os.waitpid(int(first), 0)
        self.assertFalse(T.server_alive())

    def test_open_port_means_no_spawn(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", self.port))
        srv.listen(1)
        try:
            self.server("raise SystemExit('should not run')\n")
            self.assertFalse(T.ensure_dashboard())
            self.assertIsNone(self.pid())
        finally:
            srv.close()

    def test_listener_of_this_workspace_counts_despite_a_stale_pid(self):
        self.state.mkdir()
        (self.state / "dashboard.pid").write_text("999999")
        self.server(LISTENER)
        p = self.spawn(self.root / "dashboard" / "server.py", self.port)
        self.assertTrue(T.server_alive())
        self.assertEqual(T.server_pids(), [p.pid])

    def test_listener_of_another_workspace_does_not_count(self):
        self.server(LISTENER)
        self.foreign(self.port)
        self.assertFalse(T.server_alive())
        self.assertEqual(T.server_pids(), [])

    def test_stop_kills_only_this_workspace(self):
        self.server(LISTENER)
        other = self.foreign(self.port)
        mine = self.spawn(self.root / "dashboard" / "server.py", free_port())
        self.state.mkdir()
        (self.state / "dashboard.pid").write_text(str(mine.pid))
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            self.dashboard("stop")
        self.assertEqual(out.getvalue(), f"stopped {mine.pid}\n")
        mine.wait(5)
        self.assertIsNone(other.poll())
        self.assertIsNone(self.pid())

    def test_start_fails_when_another_program_holds_the_port(self):
        self.server(LISTENER)
        other = self.foreign(self.port)
        with self.assertRaises(SystemExit) as e:
            self.dashboard("start")
        self.assertIn(f"failed: 127.0.0.1:{self.port} is held by another program (pid {other.pid}", str(e.exception))

    def test_start_fails_when_the_child_dies(self):
        self.server("import sys\nsys.exit(1)\n")
        with self.assertRaises(SystemExit) as e:
            self.dashboard("start")
        self.assertIn("failed: the dashboard did not start; see ", str(e.exception))

    def test_start_reports_a_running_dashboard(self):
        self.server(LISTENER.replace("int(sys.argv[1])", str(self.port)))
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            self.dashboard("start")
        self.assertEqual(out.getvalue(), f"started http://127.0.0.1:{self.port}/\n")
        pid = int(self.pid())
        os.kill(pid, 9)
        os.waitpid(pid, 0)

    def test_stale_pid_of_another_program_does_not_count(self):
        self.state.mkdir()
        sleeper = subprocess.Popen(["sleep", "30"])
        self.procs.append(sleeper)
        (self.state / "dashboard.pid").write_text(str(sleeper.pid))
        self.assertFalse(T.server_alive())


class Answer(http.server.BaseHTTPRequestHandler):
    server_version = "team-dashboard"
    sys_version = ""

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


class BackendWatchTest(unittest.TestCase):
    def watch(self, wanted, retry=0.3):
        self.stop = threading.Event()
        self.err = io.StringIO()
        self.patches = [mock.patch.object(S, "wanted_backend", side_effect=wanted), mock.patch.object(sys, "stderr", self.err)]
        for p in self.patches:
            p.start()
        self.thread = threading.Thread(target=S.backend_watch, args=(self.stop, 0.05, retry), daemon=True)
        self.thread.start()

    def tearDown(self):
        self.stop.set()
        self.thread.join(5)
        for p in reversed(self.patches):
            p.stop()

    def test_retries_until_the_port_is_free(self):
        port = free_port()
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", port))
        blocker.listen(1)
        self.watch(lambda: port)
        time.sleep(1)
        blocker.close()
        self.assertEqual(self.err.getvalue().count("cannot open the backend listener"), 1)
        for _ in range(50):
            if "backend listener for the local domain" in self.err.getvalue():
                break
            time.sleep(0.1)
        self.assertTrue(T.port_open(port))
        self.stop.set()
        self.thread.join(5)
        self.assertFalse(T.port_open(port))

    def test_errors_never_stop_the_watcher(self):
        calls = []

        def wanted():
            calls.append(1)
            if len(calls) < 5:
                raise RuntimeError("boom")
            return 70000
        self.watch(wanted)
        time.sleep(1)
        self.assertTrue(self.thread.is_alive())
        self.assertEqual(self.err.getvalue().count("backend watcher error: RuntimeError('boom')"), 1)
        self.assertEqual(self.err.getvalue().count("cannot open the backend listener on 127.0.0.1:70000"), 1)
        self.assertGreater(len(calls), 10)


class StatusTest(unittest.TestCase):
    def test_domain_answers_ignores_proxies(self):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Answer)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with mock.patch.dict(os.environ, {"http_proxy": f"http://127.0.0.1:{free_port()}", "HTTP_PROXY": "", "no_proxy": "", "NO_PROXY": ""}):
                self.assertTrue(team.domain_answers(f"http://127.0.0.1:{srv.server_address[1]}/"))
        finally:
            srv.shutdown()
            srv.server_close()

    def test_domain_answers_checks_the_server_header(self):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Answer)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            self.assertTrue(team.domain_answers(f"http://127.0.0.1:{srv.server_address[1]}/"))
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertFalse(team.domain_answers(f"http://127.0.0.1:{free_port()}/"))

    def test_dashboard_state_names_what_answers(self):
        port = free_port()
        with mock.patch.object(T, "PORT", port), mock.patch.object(T, "dashboard_host", return_value="mission.control"), \
                mock.patch.object(team, "domain_answers", return_value=True):
            up, detail = team.dashboard_state()
        self.assertTrue(up)
        self.assertIn(f"direct http://127.0.0.1:{port}/ does not answer", detail)
        self.assertIn("domain http://mission.control/ answers", detail)
        with mock.patch.object(T, "PORT", port), mock.patch.object(T, "dashboard_host", return_value=None):
            up, detail = team.dashboard_state()
        self.assertFalse(up)
        srv = socket.socket()
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        try:
            with mock.patch.object(T, "PORT", port), mock.patch.object(T, "dashboard_host", return_value=None):
                up, detail = team.dashboard_state()
        finally:
            srv.close()
        self.assertTrue(up)
        self.assertEqual(detail, f"direct http://127.0.0.1:{port}/ answers")


if __name__ == "__main__":
    unittest.main()
