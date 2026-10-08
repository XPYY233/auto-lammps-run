#!/usr/bin/env python3
"""Supervise one desktop entry: start/reuse the local service and open the page.

The entry bundle runs this process in the foreground, so the entry stays alive while the page
is open. It starts or reuses exactly the configured service through `scripts/launch_local.py`.
Closing the page or desktop entry leaves that service running: its in-process research,
execution, collection and analysis workers must survive browser closure. The entry exits
after the page's close beacon. Only an entry whose page never connected stops a service it
just started, through the identity-checked `scripts/stop_local.py`. Task records and ledgers
are untouched. An older service without activity endpoints is left running.

用法：python3 scripts/desktop_supervisor.py --config <产品部署/本地启动器/current.json>
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

sys.path.insert(0, str(Path(__file__).resolve().parent))
import launch_local

HERE = Path(__file__).resolve().parent


def session_url(url, token):
    """Insert the session token into the launcher URL without disturbing `?release=`/#view."""
    head, _, fragment = url.partition('#')
    return f"{head}{'&' if '?' in head else '?'}session={token}#{fragment}"


class Watch:
    """Decide whether this entry's page is still open; kept pure for offline tests.

    States: `waiting` (no report yet, first-open grace), `connected`, `pending` (gone but still
    inside its grace window), `stop`.
    """

    def __init__(self, token, *, visible_stale=30.0, hidden_stale=150.0, close_grace=12.0):
        self.token = token
        self.visible_stale = visible_stale
        self.hidden_stale = hidden_stale
        self.close_grace = close_grace
        self.seen = False
        self.absent_since = None
        self.absent_limit = visible_stale

    def observe(self, snapshot, now):
        sessions = snapshot.get('sessions') or {}
        entry = sessions.get(self.token)
        if entry is not None:
            self.seen = True
            self.absent_since = None
            age = max(0.0, now - float(entry.get('at') or 0))
            limit = self.hidden_stale if entry.get('hidden') else self.visible_stale
            return 'connected' if age <= limit else 'stop'
        if not self.seen:
            return 'waiting'
        if self.absent_since is None:
            # A close beacon means the page really unloaded; otherwise the report just expired.
            # Either way a short grace first, so a reload or a quick re-open is not punished.
            self.absent_since = now
            self.absent_limit = (self.close_grace if snapshot.get('closed_session') == self.token
                                 else self.visible_stale)
        return 'stop' if now - self.absent_since >= self.absent_limit else 'pending'


def read_activity(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'sessions': {}}
    return data if isinstance(data, dict) else {'sessions': {}}


def activity_enabled(config):
    """Return the live snapshot only when the service really serves the activity endpoints."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{config['port']}/api/session/activity", timeout=2) as response:
            data = json.load(response)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get('enabled') else None


def port_listening(port):
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex(('127.0.0.1', port)) == 0


def stop_service(config_path):
    """Stop only the identity-matched service, through the shared stopper."""
    result = subprocess.run([sys.executable, '-I', str(HERE / 'stop_local.py'), '--config', str(config_path)],
                            capture_output=True, text=True)
    return result.returncode, (result.stdout + result.stderr).strip()


class Journal:
    def __init__(self, path):
        self.path = Path(path)

    def write(self, text):
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n"
        try:
            with self.path.open('a', encoding='utf-8') as stream:
                stream.write(line)
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        if sys.stderr.isatty():  # the entry already redirects the log file to stderr
            sys.stderr.write(line)


def announce(title, message, *, alert=False, quiet=False):
    if quiet or sys.platform != 'darwin':
        return
    script = (f'display alert {json.dumps(title)} message {json.dumps(message)} giving up after 45'
              if alert else f'display notification {json.dumps(message)} with title {json.dumps(title)}')
    try:
        subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass


def parse(argv=None):
    parser = argparse.ArgumentParser(description='监督桌面入口：启动/复用本地服务，关页后保留后台工作')
    parser.add_argument('--config', required=True, type=Path, help='启动器 current.json 路径')
    parser.add_argument('--activity-file', type=Path, help='页面活动文件；默认放在运行记录目录')
    parser.add_argument('--session-token', help='本次入口的页面标识；默认随机生成')
    parser.add_argument('--connect-seconds', type=float, default=90.0, help='首次打开等待页面心跳的宽限期')
    parser.add_argument('--stale-seconds', type=float, default=30.0, help='页面可见时超过该秒数无心跳即视为关闭')
    parser.add_argument('--hidden-stale-seconds', type=float, default=150.0,
                        help='页面在后台时的宽限（浏览器会节流后台计时器）')
    parser.add_argument('--close-grace-seconds', type=float, default=12.0,
                        help='收到关闭信标后的短暂宽限，避免刷新或立刻重开被误判')
    parser.add_argument('--delegate-wait-seconds', type=float, default=15.0,
                        help='已有入口时的等待时间；超时则只复用服务并打开页面')
    parser.add_argument('--poll-seconds', type=float, default=2.0)
    parser.add_argument('--no-browser', action='store_true', help='只监督，不打开浏览器（测试用）')
    parser.add_argument('--no-dialogs', action='store_true', help='不显示系统通知/提示（测试用）')
    parser.add_argument('--no-stop', action='store_true', help='不调用停止器（仅离线测试可用）')
    return parser.parse_args(argv)


def acquire_lock(descriptor, wait_seconds):
    """Take the entry lock now, or wait briefly for the current entry to finish."""
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.5)


def open_page(url, journal):
    """Open the page in its own process, never inside this supervisor.

    ``webbrowser`` waits for the browser it launches, and forking a supervisor that may have
    threads is not safe; a detached helper process keeps both concerns away from the watch loop
    (and keeps the page opening even after this entry exits).
    """
    opener = 'import sys, webbrowser; sys.exit(0 if webbrowser.open(sys.argv[1]) else 1)'
    # -I keeps the helper isolated; an explicit BROWSER override must still reach webbrowser.
    flags = [] if os.environ.get('BROWSER') else ['-I']
    try:
        subprocess.Popen([sys.executable, *flags, '-c', opener, url], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as error:
        journal.write(f'浏览器启动失败（{error}）；请手动访问：{url}')


def supervise(args):
    quiet = args.no_dialogs or bool(os.environ.get('AUTO_LAMMPS_SILENT'))
    config_path = Path(args.config).expanduser().resolve()
    try:
        config = launch_local.read_config(config_path)
    except (launch_local.LaunchError, OSError, ValueError) as error:
        # Nothing was started from this configuration, so nothing may be stopped either.
        announce('Auto-LAMMPS 未能启动', str(error), alert=True, quiet=quiet)
        return 1
    state = Path(config['state_directory']).expanduser()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    journal = Journal(state / 'desktop-entry.log')
    receipt_path = state / 'last-desktop-entry.json'
    record = {'release': config.get('release', ''), 'port': config['port'], 'entry': 'single'}

    def save(**extra):
        record.update(extra)
        record['checked_at'] = time.time()
        temporary = receipt_path.with_name(receipt_path.name + '.tmp')
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        os.chmod(temporary, 0o600)
        os.replace(temporary, receipt_path)

    # A raw descriptor: the lock lives until this process exits, without an unclosed file object.
    descriptor = os.open(state / 'desktop-entry.lock', os.O_CREAT | os.O_RDWR, 0o600)
    if not acquire_lock(descriptor, args.delegate_wait_seconds):
        # A long-running entry already supervises this application: reuse the service and page.
        # This instance owns nothing, so it must not stop the service that entry started.
        result = launch_local.launch(config, open_browser=False)
        save(role='delegate', started=result['started'], url=result['url'], monitoring='primary-entry', stopped=False)
        journal.write(f"已有入口在监督本应用；本次只复用并打开页面：{result['url']}")
        if not args.no_browser:
            # The helper process outlives this delegate entry, so the page still appears.
            open_page(result['url'], journal)
        return 0

    activity_path = Path(args.activity_file).expanduser() if args.activity_file else state / 'session-activity.json'
    token = args.session_token or secrets.token_hex(8)
    supervised = dict(config)
    if '--session-activity-file' not in supervised['args']:
        supervised['args'] = [*supervised['args'], '--session-activity-file', str(activity_path)]
    # 瞬时冲突（端口刚释放、上一次启动仍在收尾、并发点击）不应表现为"弹窗闪退"：
    # 有界慢重试，只有连续失败才提示。
    result, last_error = None, None
    for attempt in range(3):
        try:
            result = launch_local.launch(supervised, open_browser=False)
            last_error = None
            break
        except (launch_local.LaunchError, OSError, ValueError) as error:
            last_error = error
            if attempt < 2:
                journal.write(f'启动未完成（第 {attempt + 1} 次），稍后重试：{error}')
                time.sleep(4)
    if result is None:
        save(role='supervisor', error=str(last_error), stopped=False)
        journal.write('启动未完成：' + str(last_error))
        announce('Auto-LAMMPS 未能启动', str(last_error), alert=True, quiet=quiet)
        return 1
    if activity_enabled(config) is None:
        message = ('正在运行的服务未启用页面活动记录（较早的安装）：本次不自动停止，也不重启或关闭它。'
                   if not result['started'] else '本次启动的服务未启用页面活动记录，未自动停止本次服务。')
        save(role='supervisor', started=result['started'], url=result['url'],
             monitoring='unavailable', stopped=False)
        journal.write(message)
        announce('Auto-LAMMPS 无法自动停止', message, quiet=quiet)
        return 0 if not result['started'] else 1

    url = session_url(result['url'], token)
    Path(activity_path).unlink(missing_ok=True)
    if not args.no_browser:
        open_page(url, journal)
    watcher = Watch(token, visible_stale=args.stale_seconds, hidden_stale=args.hidden_stale_seconds,
                    close_grace=args.close_grace_seconds)
    # Keep the service after a real page has connected: its workers are independent of the
    # browser. A page that never appears may only clean up a service started by this entry.
    owner = {'stop': bool(result['started'])}
    save(role='supervisor', started=result['started'], url=url, session=token,
         monitoring='active', adopted=owner['stop'], stopped=False)
    journal.write(f"入口已就绪（{'本次启动' if result['started'] else '复用已有服务'}）：{url}")

    asked = {'exit': False}

    def request_exit(signum, frame):
        asked['exit'] = True
        journal.write(f'收到退出信号 {signum}；本地后台服务继续运行。')

    previous = {name: signal.signal(getattr(signal, name), request_exit)
                for name in ('SIGINT', 'SIGTERM', 'SIGHUP')}
    outcome = {'reason': 'unknown', 'stopped': False, 'stopper': ''}
    try:
        outcome['reason'] = watch_loop(args, config, watcher, activity_path, journal, asked, owner)
    finally:
        for name, handler in previous.items():
            signal.signal(getattr(signal, name), handler)
        if not args.no_stop and owner['stop'] and outcome['reason'] == 'page_never_connected':
            try:
                code, output = stop_service(config_path)
                outcome['stopped'] = code == 0
                outcome['stopper'] = output.splitlines()[-1] if output else ''
                journal.write('停止结果：' + (output or f'退出码 {code}'))
            except OSError as error:
                journal.write('停止器无法执行：' + str(error))

    reason = outcome['reason']
    messages = {
        'page_closed': '已检测到网页关闭，本地服务继续跟进任务。重新打开应用即可查看进度和结果。',
        'page_never_connected': '页面在宽限期内没有连接，本次启动的本地服务已停止，没有留下后台进程。',
        'app_exit': '桌面入口已退出，本地服务继续跟进任务。重新打开应用即可查看进度和结果。',
        'service_gone': '本地服务已自行退出或异常结束，入口随之关闭；任务记录、账本与运行记录保留。',
    }
    if reason == 'reused_page_missing':
        text = '复用的服务没有收到页面心跳；按“不误杀”原则保留该服务，未做任何关闭操作。'
        journal.write(text)
        announce('Auto-LAMMPS 未自动停止', text, quiet=quiet)
        save(role='supervisor', started=result['started'], url=url, session=token,
             monitoring='unavailable', reason=reason, stopped=False)
        return 0
    text = messages.get(reason, f'监督结束：{reason}')
    journal.write(text)
    announce('Auto-LAMMPS 本地服务已结束' if reason in {'service_gone', 'page_never_connected'} else 'Auto-LAMMPS 后台继续运行',
             text, alert=reason in {'service_gone', 'page_never_connected'}, quiet=quiet)
    save(role='supervisor', started=result['started'], url=url, session=token, monitoring='active',
         reason=reason, stopped=outcome['stopped'], stopper=outcome['stopper'])
    return 1 if reason == 'service_gone' else 0


def watch_loop(args, config, watcher, activity_path, journal, asked, owner):
    """Return the reason that ends supervision."""
    now = time.time()
    connect_deadline = now + args.connect_seconds
    last_poll = now
    while True:
        time.sleep(args.poll_seconds)
        now = time.time()
        gap, last_poll = now - last_poll, now
        if asked['exit']:
            return 'app_exit'
        if not port_listening(config['port']):
            return 'service_gone'
        if gap > max(3 * args.poll_seconds, 10.0):
            # Suspend/wake or a long stall: neither side could report, so re-arm instead of judging.
            journal.write(f'检测到 {gap:.0f} 秒无轮询（休眠/唤醒或长时间挂起）；重新计时，不据此判定关闭。')
            connect_deadline = now + args.connect_seconds
            continue
        state = watcher.observe(read_activity(activity_path), now)
        if state == 'connected':
            owner['stop'] = True
            continue
        if state == 'waiting':
            if now >= connect_deadline:
                return 'page_never_connected' if owner['stop'] else 'reused_page_missing'
            continue
        if state == 'stop':
            return 'page_closed'


def main(argv=None):
    """Exit codes the entry wrapper relies on: 0/1 mean "nothing was left running"."""
    os.umask(0o077)
    try:
        return supervise(parse(argv))
    except SystemExit:
        raise
    except BaseException:  # an unexpected crash must be visible, not a silent exit
        import traceback
        traceback.print_exc()
        return 4


if __name__ == '__main__':
    sys.exit(main())
