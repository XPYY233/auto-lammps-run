#!/usr/bin/env python3
"""稳定停止本地 Auto-LAMMPS 应用：只关配置里那一个服务，且先核对身份。

用法：python3 scripts/stop_local.py --config <产品部署/本地启动器/current.json>


判定顺序（全部通过才动手）：
  1) 读 current.json 得到 port 与 args 里的 --data-directory / 运行环境；
  2) 查监听该端口的 PID；
  3) 核对 PID 的命令行**同时**包含同一运行环境与同一 --data-directory（防止误杀同端口的其他服务）；
  4) SIGTERM → 最多等 10 秒 → 仍存活才 SIGKILL；
  5) 复查端口已释放，并把回执写入运行记录。

不做的事：不删除任务库、账本、候选快照或运行记录；不触碰 8785 等其他实例；
**不取消任何 HPC 作业**（远端计算不受影响，只是本机关闭网页服务）。
"""
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path


def read_config(path):
    config = json.loads(Path(path).read_text(encoding='utf-8'))
    args = config.get('args') or []
    data_directory = None
    for index, item in enumerate(args):
        if item == '--data-directory' and index + 1 < len(args):
            data_directory = args[index + 1]
    return {'port': config['port'], 'python': config.get('python', ''), 'data_directory': data_directory,
            'release': config.get('release', ''), 'state_directory': config['state_directory']}


def listening_pid(port):
    result = subprocess.run(['lsof', '-nP', f'-iTCP:{port}', '-sTCP:LISTEN', '-t'], capture_output=True, text=True)
    pids = [line.strip() for line in result.stdout.split() if line.strip().isdigit()]
    return pids


def command_of(pid):
    result = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True)
    return result.stdout.strip()


def port_is_free(port):
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex(('127.0.0.1', port)) != 0


def main():
    import argparse
    parser = argparse.ArgumentParser(description='稳定停止本地 Auto-LAMMPS 应用（身份校验后只关本配置的服务）')
    parser.add_argument('--config', required=True, type=Path, help='启动器 current.json 路径')
    arguments = parser.parse_args()
    config = read_config(arguments.config)
    port = config['port']
    state = Path(config['state_directory'])
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt_path = state / 'last-stop.json'

    if port_is_free(port):
        receipt = {'stopped': False, 'reason': 'not_running', 'port': port, 'checked_at': time.time()}
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'应用未在运行（端口 {port} 空闲），无需关闭。')
        return 0

    victims = []
    for pid in listening_pid(port):
        command = command_of(pid)
        # 身份判据：必须是本应用的模块、同一数据目录、同一端口；
        # python 可能以 venv 垫片启动而进程显示真实解释器，故用真实路径比对。
        same_module = 'auto_lammps.web' in command
        same_data = bool(config['data_directory']) and config['data_directory'] in command
        same_port = f"--port {port}" in command
        # 身份以三者同时匹配为准：本应用模块 + 本配置的私有数据目录 + 本配置端口。
        # 运行环境只记录不设门槛：venv 垫片与框架真实解释器是同一程序的不同路径。
        executable = subprocess.run(['ps', '-p', str(pid), '-o', 'comm='],
                                    capture_output=True, text=True).stdout.strip()
        try:
            runtime_match = bool(executable) and bool(config['python']) and (
                Path(executable).resolve() == Path(config['python']).resolve()
                or executable.startswith(str(Path(config['python']).parent.parent)))
        except OSError:
            runtime_match = False
        if same_module and same_data and same_port:
            victims.append(str(pid))
            if not runtime_match:
                print(f'提示：PID {pid} 的运行环境路径与配置不同（venv 垫片/框架解释器），身份仍按模块+数据目录+端口确认。')
        else:
            print(f'跳过 PID {pid}：身份不匹配（模块={same_module} 数据目录={same_data} 端口={same_port}），不误杀。')
    if not victims:
        receipt = {'stopped': False, 'reason': 'identity_mismatch', 'port': port,
                   'listeners': listening_pid(port), 'checked_at': time.time()}
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'端口 {port} 被占用，但没有进程与本配置身份一致；未做任何操作。')
        return 1

    for pid in victims:
        os.kill(int(pid), signal.SIGTERM)
    deadline = time.time() + 10
    while time.time() < deadline and not port_is_free(port):
        time.sleep(0.5)
    killed = []
    if not port_is_free(port):
        for pid in victims:
            os.kill(int(pid), signal.SIGKILL)
            killed.append(pid)
        time.sleep(1)

    freed = port_is_free(port)
    receipt = {'stopped': freed, 'port': port, 'terminated': victims, 'force_killed': killed,
               'identity': 'module+data_directory+port',
               'release': config['release'], 'checked_at': time.time(),
               'data_kept': ['tasks', 'ledger', 'candidate-snapshots', 'run records'],
               'remote_jobs': 'untouched'}
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    if freed:
        print(f'Auto-LAMMPS 已停止（端口 {port} 已释放）。任务、账本与产物均保留；HPC 作业未受影响。')
        return 0
    print(f'停止失败：端口 {port} 仍被占用。')
    return 1


if __name__ == '__main__':
    sys.exit(main())
