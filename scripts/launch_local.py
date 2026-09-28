"""Open an explicitly configured local candidate without replacing other services.

The private JSON file contains python, args (web CLI arguments), port, release,
asset_sha256 (app.js/app.css), and state_directory. It contains no API secrets.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.request
import webbrowser


class LaunchError(RuntimeError):
    pass


def read_config(path):
    config = json.loads(Path(path).read_text(encoding='utf-8'))
    port = config.get('port')
    if type(port) is not int or not 1024 <= port <= 65535:
        raise LaunchError('启动配置中的端口无效。')
    args = config.get('args')
    if not isinstance(args, list) or not all(isinstance(v, str) for v in args):
        raise LaunchError('启动参数无效。')
    if args.count('--port') != 1 or args[args.index('--port') + 1:args.index('--port') + 2] != [str(port)]:
        raise LaunchError('启动端口与网页地址不一致。')
    if '--host' in args or '--reload' in args:
        raise LaunchError('本地启动器仅支持固定的本机服务。')
    for key in ('python', 'state_directory'):
        if not isinstance(config.get(key), str) or not Path(config[key]).is_absolute():
            raise LaunchError('启动配置需要明确的安装和记录目录。')
    if not Path(config['python']).is_file():
        raise LaunchError('应用运行环境不存在，请恢复已安装的应用。')
    digests = config.get('asset_sha256', {})
    if set(digests) != {'app.js', 'app.css'} or any(
        not isinstance(v, str) or len(v) != 64 or any(c not in '0123456789abcdef' for c in v)
        for v in digests.values()
    ):
        raise LaunchError('缺少已安装界面的版本凭据。')
    return config


def matching_service(config):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = f"http://127.0.0.1:{config['port']}/"
    try:
        with opener.open(base + 'api/schema', timeout=2) as response:
            data = json.load(response)
        if not isinstance(data, dict) or 'candidate_preparation' not in data:
            return False
        for name, expected in config['asset_sha256'].items():
            with opener.open(base + 'assets/' + name, timeout=2) as response:
                if hashlib.sha256(response.read()).hexdigest() != expected:
                    return False
        return True
    except (OSError, ValueError):
        return False


def occupied(port):
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex(('127.0.0.1', port)) == 0


def launch(config, open_browser=True, timeout=30):
    state = Path(config['state_directory'])
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state, 0o700)
    lock = os.open(state / 'launcher.lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(lock, 'w') as stream:
        # Concurrent desktop clicks serialize; none can submit or duplicate jobs.
        fcntl.flock(stream, fcntl.LOCK_EX)
        started = False
        if not matching_service(config):
            if occupied(config['port']):
                raise LaunchError('该端口已有不同版本或未就绪的服务。未关闭任何进程；请检查应用版本。')
            log = state / 'application.log'
            fd = os.open(log, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            env = dict(os.environ)
            for key in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV'):
                env.pop(key, None)
            with os.fdopen(fd, 'ab') as output:
                proc = subprocess.Popen([config['python'], '-I', '-m', 'auto_lammps.web', *config['args']],
                                        cwd=state, env=env, stdin=subprocess.DEVNULL,
                                        stdout=output, stderr=output, start_new_session=True)
            deadline = time.monotonic() + timeout
            while not matching_service(config):
                if proc.poll() is not None:
                    raise LaunchError('应用未能启动，诊断记录：' + str(log))
                if time.monotonic() >= deadline:
                    raise LaunchError('应用仍在准备，已保留进程与记录。稍后重新打开即可。')
                time.sleep(.25)
            started = True
        url = f"http://127.0.0.1:{config['port']}/?release={config['asset_sha256']['app.js'][:12]}#home"
        receipt = {'release': config.get('release', ''), 'url': url, 'started': started,
                   'checked_at': time.time(), 'ui_sha256': config['asset_sha256']}
        (state / 'last-launch.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
        os.chmod(state / 'last-launch.json', 0o600)
    if open_browser:
        webbrowser.open(url)
    return receipt


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='打开已安装的 Auto-LAMMPS 本地应用')
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    try:
        result = launch(read_config(args.config), not args.no_browser)
    except (LaunchError, OSError, ValueError) as error:
        parser.exit(1, str(error) + '\n')
    print('Auto-LAMMPS 已就绪：' + result['url'])


if __name__ == '__main__':
    main()
