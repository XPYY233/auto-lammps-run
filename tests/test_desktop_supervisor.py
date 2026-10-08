"""桌面入口监督进程的离线测试：判定“页面已关闭”的规则，以及绝不误杀的前提。

不启动服务、不打开浏览器、不调用停止器（除非显式替换为记录桩）。
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('desktop_supervisor',
                                              Path(__file__).parents[1] / 'scripts/desktop_supervisor.py')
supervisor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(supervisor)


class SessionUrlTests(unittest.TestCase):
    def test_session_token_is_inserted_before_the_view_fragment(self):
        self.assertEqual(supervisor.session_url('http://127.0.0.1:8787/?release=abc#home', 'deadbeef'),
                         'http://127.0.0.1:8787/?release=abc&session=deadbeef#home')

    def test_url_without_query_still_gets_a_parameter(self):
        self.assertEqual(supervisor.session_url('http://127.0.0.1:8787/#home', 'x'),
                         'http://127.0.0.1:8787/?session=x#home')


class WatchTests(unittest.TestCase):
    """The decision function that decides when the page counts as gone."""

    def setUp(self):
        self.watch = supervisor.Watch('token', visible_stale=30, hidden_stale=150, close_grace=12)

    def test_first_open_has_a_grace_period(self):
        self.assertEqual(self.watch.observe({'sessions': {}}, 1000), 'waiting')

    def test_fresh_heartbeat_keeps_the_service(self):
        self.assertEqual(self.watch.observe({'sessions': {'token': {'at': 1000, 'hidden': False}}}, 1000), 'connected')

    def test_expired_visible_heartbeat_stops_the_service(self):
        self.watch.observe({'sessions': {'token': {'at': 1000, 'hidden': False}}}, 1000)
        self.assertEqual(self.watch.observe({'sessions': {'token': {'at': 1000, 'hidden': False}}}, 1031), 'stop')

    def test_background_page_gets_the_longer_limit(self):
        self.watch.observe({'sessions': {'token': {'at': 1000, 'hidden': True}}}, 1000)
        self.assertEqual(self.watch.observe({'sessions': {'token': {'at': 1000, 'hidden': True}}}, 1100), 'connected')
        self.assertEqual(self.watch.observe({'sessions': {'token': {'at': 1000, 'hidden': True}}}, 1151), 'stop')

    def test_close_beacon_waits_a_short_grace_then_stops(self):
        self.watch.observe({'sessions': {'token': {'at': 1000, 'hidden': False}}}, 1000)
        snapshot = {'sessions': {}, 'closed_session': 'token'}
        self.assertEqual(self.watch.observe(snapshot, 1001), 'pending')
        self.assertEqual(self.watch.observe(snapshot, 1012), 'pending')
        self.assertEqual(self.watch.observe(snapshot, 1014), 'stop')

    def test_reload_during_the_close_grace_is_not_punished(self):
        self.watch.observe({'sessions': {'token': {'at': 1000, 'hidden': False}}}, 1000)
        self.assertEqual(self.watch.observe({'sessions': {}, 'closed_session': 'token'}, 1001), 'pending')
        self.assertEqual(self.watch.observe({'sessions': {'token': {'at': 1002, 'hidden': False}}}, 1002), 'connected')
        self.assertEqual(self.watch.observe({'sessions': {'token': {'at': 1003, 'hidden': False}}}, 1004), 'connected')

    def test_a_page_that_never_reported_is_never_treated_as_closed_by_itself(self):
        self.assertEqual(self.watch.observe({'sessions': {}}, 1000), 'waiting')
        self.assertEqual(self.watch.observe({'sessions': {}}, 5000), 'waiting')
        self.assertFalse(self.watch.seen)


class SuperviseTests(unittest.TestCase):
    """Full supervision decisions, with the launcher/stopper replaced by recording stubs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.config_path = self.state / 'current.json'
        self.config_path.write_text(json.dumps({
            'python': sys.executable, 'port': 8799, 'release': 'synthetic-test',
            'state_directory': str(self.state), 'args': ['--data-directory', str(self.state), '--port', '8799'],
            'asset_sha256': {name: hashlib.sha256(name.encode()).hexdigest() for name in ('app.js', 'app.css')},
        }), encoding='utf-8')
        self.stops = []

    def args(self, **overrides):
        base = dict(config=self.config_path, activity_file=self.state / 'session-activity.json',
                    session_token='tok', connect_seconds=0.2, stale_seconds=0.2,
                    hidden_stale_seconds=0.4, close_grace_seconds=0.1, delegate_wait_seconds=0.1,
                    poll_seconds=0.02, no_browser=True, no_dialogs=True, no_stop=False)
        base.update(overrides)
        return mock.Mock(**base)

    def run_supervise(self, *, started, activity, enabled=True, snapshots=(), no_stop=False,
                      browser_opens=False, browser_side_effect=None):
        launch = mock.Mock(return_value={'started': started, 'url': 'http://127.0.0.1:8799/?release=abc#home',
                                         'release': 'synthetic-test'})
        sequence = list(snapshots)

        def read(path):
            return sequence.pop(0) if sequence else activity

        with mock.patch.object(supervisor.launch_local, 'launch', launch), \
             mock.patch.object(supervisor, 'activity_enabled', return_value=({'enabled': True} if enabled else None)), \
             mock.patch.object(supervisor, 'port_listening', return_value=True), \
             mock.patch.object(supervisor, 'read_activity', side_effect=read), \
             mock.patch.object(supervisor, 'stop_service', side_effect=lambda path: (self.stops.append(str(path)), (0, '已停止'))[1]), \
             mock.patch.object(supervisor.webbrowser, 'open', side_effect=browser_side_effect) as browser:
            code = supervisor.supervise(self.args(no_browser=not browser_opens, no_stop=no_stop))
        return code, launch, browser

    def resolved_config(self):
        return str(self.config_path.resolve())

    def receipt(self):
        return json.loads((self.state / 'last-desktop-entry.json').read_text(encoding='utf-8'))

    def test_close_beacon_preserves_service_for_background_research(self):
        closed = {'sessions': {}, 'closed_session': 'tok'}
        code, launch, browser = self.run_supervise(
            started=True, activity=closed,
            snapshots=[{'sessions': {'tok': {'at': time.time(), 'hidden': False}}}])
        self.assertEqual(code, 0)
        self.assertEqual(self.stops, [])
        self.assertEqual(self.receipt()['reason'], 'page_closed')
        self.assertFalse(self.receipt()['stopped'])
        self.assertFalse(browser.called)  # --no-browser keeps the test headless

    def test_close_leaves_an_isolated_real_service_process_alive(self):
        """A real local listener must outlive the entry when the page closes."""
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        code = ('from http.server import BaseHTTPRequestHandler, HTTPServer; '
                'import sys; HTTPServer(("127.0.0.1", int(sys.argv[1])), BaseHTTPRequestHandler).serve_forever()')
        service = subprocess.Popen([sys.executable, '-c', code, str(port)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (service.terminate(), service.wait(timeout=5)) if service.poll() is None else None)
        for _ in range(100):
            if supervisor.port_listening(port):
                break
            time.sleep(0.02)
        self.assertTrue(supervisor.port_listening(port))
        config={'port':port,'state_directory':str(self.state),'args':[],'release':'synthetic-test'}
        first=iter([{'sessions': {'tok': {'at': time.time(), 'hidden': False}}}])
        closed={'sessions': {}, 'closed_session': 'tok'}
        with mock.patch.object(supervisor.launch_local, 'read_config', return_value=config), \
             mock.patch.object(supervisor.launch_local, 'launch',
                               return_value={'started':True,'url':f'http://127.0.0.1:{port}/#home'}), \
             mock.patch.object(supervisor, 'activity_enabled', return_value={'enabled':True}), \
             mock.patch.object(supervisor, 'read_activity', side_effect=lambda _: next(first, closed)), \
             mock.patch.object(supervisor, 'stop_service') as stopper:
            result=supervisor.supervise(self.args(no_browser=True))
        self.assertEqual(result,0)
        self.assertEqual(self.receipt()['reason'],'page_closed')
        stopper.assert_not_called()
        self.assertIsNone(service.poll())
        self.assertTrue(supervisor.port_listening(port))

    def test_started_service_is_stopped_when_the_page_never_connects(self):
        code, launch, browser = self.run_supervise(started=True, activity={'sessions': {}})
        self.assertEqual(code, 0)
        self.assertEqual(self.stops, [self.resolved_config()])
        self.assertEqual(self.receipt()['reason'], 'page_never_connected')

    def test_a_reused_service_is_kept_when_its_page_never_reports(self):
        code, launch, browser = self.run_supervise(started=False, activity={'sessions': {}})
        self.assertEqual(code, 0)
        self.assertEqual(self.stops, [])
        self.assertEqual(self.receipt()['reason'], 'reused_page_missing')
        self.assertFalse(self.receipt()['stopped'])

    def test_an_older_service_without_the_endpoints_is_left_running(self):
        code, launch, browser = self.run_supervise(started=False, activity={'sessions': {}}, enabled=False)
        self.assertEqual(code, 0)
        self.assertEqual(self.stops, [])
        self.assertEqual(self.receipt()['monitoring'], 'unavailable')

    def test_no_stop_flag_keeps_unopened_service_running_for_offline_tests(self):
        code, launch, browser = self.run_supervise(started=True, activity={'sessions': {}}, no_stop=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.stops, [])
        self.assertFalse(self.receipt()['stopped'])

    def test_the_page_is_opened_by_a_separate_process(self):
        """The stdlib waits for a generic browser process; supervision must not wait with it."""
        closed = {'sessions': {}, 'closed_session': 'tok'}
        first = iter([{'sessions': {'tok': {'at': time.time(), 'hidden': False}}}])
        with mock.patch.object(supervisor.subprocess, 'Popen') as opener, \
             mock.patch.object(supervisor.launch_local, 'launch',
                               return_value={'started': True, 'url': 'http://127.0.0.1:8799/?release=abc#home'}), \
             mock.patch.object(supervisor, 'activity_enabled', return_value={'enabled': True}), \
             mock.patch.object(supervisor, 'port_listening', return_value=True), \
             mock.patch.object(supervisor, 'read_activity', side_effect=lambda _: next(first, closed)), \
             mock.patch.object(supervisor, 'stop_service', side_effect=lambda path: (self.stops.append(str(path)), (0, '已停止'))[1]):
            code = supervisor.supervise(self.args(no_browser=False))
        self.assertEqual(code, 0)
        self.assertEqual(self.stops, [])
        self.assertIn('session=', opener.call_args.args[0][-1])
        self.assertTrue(opener.call_args.kwargs['start_new_session'])


    def test_a_second_entry_delegates_without_stopping_the_running_service(self):
        """A second click reuses the service and its page; it must not stop what it did not start."""
        with mock.patch.object(supervisor, 'acquire_lock', return_value=False), \
             mock.patch.object(supervisor.launch_local, 'launch',
                               return_value={'started': False, 'url': 'http://127.0.0.1:8799/#home'}) as launch, \
             mock.patch.object(supervisor, 'stop_service') as stopper, \
             mock.patch.object(supervisor.subprocess, 'Popen') as browser:
            code = supervisor.supervise(self.args(no_browser=False))
        self.assertEqual(code, 0)
        stopper.assert_not_called()
        self.assertEqual(self.receipt()['role'], 'delegate')
        self.assertFalse(launch.call_args.kwargs['open_browser'])
        self.assertTrue(browser.called)


if __name__ == '__main__':
    unittest.main()
