"""Versioned operator SSH settings. Saving never changes or submits a job."""
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import uuid
from datetime import datetime, timezone

from .manifest import canonical, private_directory
from .runtime_launcher import read_regular
from .tasks import TaskError, StaleTask


def validate(value):
    fields = {'label','host','port','username','work_directory','partition','account','authentication'}
    if set(value) != fields:
        raise TaskError('请检查 HPC 连接字段。')
    if not isinstance(value['label'],str) or not 1 <= len(value['label'].strip()) <= 80:
        raise TaskError('请填写连接名称。')
    host = value['host']
    if not isinstance(host,str) or len(host)>253:
        raise TaskError('请填写主机名或 IP 地址，不包含协议、用户名或路径。')
    try: ipaddress.ip_address(host)
    except ValueError:
        if not host or any(not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', part) for part in host.split('.')):
            raise TaskError('请填写主机名或 IP 地址，不包含协议、用户名或路径。')
    if type(value['port']) is not int or not 1 <= value['port'] <= 65535:
        raise TaskError('端口应为 1–65535。')
    if not isinstance(value['username'],str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}',value['username']):
        raise TaskError('请填写 SSH 用户名。')
    path=value['work_directory']
    if not isinstance(path,str) or not re.fullmatch(r'/[A-Za-z0-9_./-]{1,510}',path) or '..' in Path(path).parts:
        raise TaskError('工作目录应为超算上的绝对路径，不含空格或上级目录。')
    for field in ('partition','account'):
        if not isinstance(value[field],str) or not re.fullmatch(r'[A-Za-z0-9_.-]{0,80}',value[field]):
            raise TaskError('分区和账户只允许字母、数字、下划线、点和短横线。')
    if value['authentication'] not in {'agent','private_key'}:
        raise TaskError('请选择 SSH 认证方式。')
    return value


def ssh_probe(argv):
    # stdout/stderr can contain server messages and paths: never reflect them.
    try:
        result=subprocess.run(argv,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL,timeout=20,check=False)
        return result.returncode == 0
    except (OSError,subprocess.TimeoutExpired): return False


class HPCConnections:
    def __init__(self,tasks,*,probe=ssh_probe):
        self.tasks,self.probe=tasks,probe
        self.directory=private_directory(tasks.path.parent/'hpc-connections')
        with tasks.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS hpc_profiles (revision INTEGER PRIMARY KEY, configuration TEXT NOT NULL, credential_id TEXT NOT NULL, at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS hpc_connection_checks (id INTEGER PRIMARY KEY, revision INTEGER NOT NULL, connected INTEGER NOT NULL, at TEXT NOT NULL)')
            for table in ('hpc_profiles','hpc_connection_checks'):
                for action in ('UPDATE','DELETE'):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, 'immutable connection history'); END")

    def _row(self,revision=None):
        with self.tasks.transaction() as db:
            return db.execute('SELECT * FROM hpc_profiles ORDER BY revision DESC LIMIT 1').fetchone() if revision is None else db.execute('SELECT * FROM hpc_profiles WHERE revision=?',(revision,)).fetchone()

    def status(self):
        row=self._row()
        if row is None: return dict(configured=False,revision=0,profile=None,last_check=None,execution_enabled=False)
        with self.tasks.transaction() as db:
            checked=db.execute('SELECT connected,at FROM hpc_connection_checks WHERE revision=? ORDER BY id DESC LIMIT 1',(row['revision'],)).fetchone()
        return dict(configured=True,revision=row['revision'],profile=json.loads(row['configuration']),
                    last_check=dict(checked) if checked else None,execution_enabled=False)

    def save(self,profile,revision,*,private_key=None,known_hosts=None,certificate=None):
        validate(profile)
        if type(revision) is not int or revision<0: raise TaskError('连接版本无效。')
        for value in (private_key,known_hosts,certificate):
            if value is not None and (not isinstance(value,str) or len(value)>32768 or '\x00' in value):
                raise TaskError('认证内容格式或长度不正确。')
        with self.tasks.transaction() as db:
            old=db.execute('SELECT * FROM hpc_profiles ORDER BY revision DESC LIMIT 1').fetchone()
            if revision != (old['revision'] if old else 0): raise StaleTask('HPC 连接已更新，请重新打开设置。')
            previous=(json.loads(read_regular(self.directory/(old['credential_id']+'.json'),100000,private=True)) if old else {})
            same_host=old and all(json.loads(old['configuration'])[k]==profile[k] for k in ('host','port','username'))
            # Changing destination does not silently copy its credentials or trust.
            if not same_host: previous={}
            secrets={k:(v if v is not None else previous.get(k,'')) for k,v in
                     (('private_key',private_key),('known_hosts',known_hosts),('certificate',certificate))}
            if profile['authentication']=='private_key' and not secrets['private_key'].strip():
                raise TaskError('请填写 SSH 私钥；加密私钥请通过控制服务的 SSH 认证代理使用。')
            if profile['authentication']=='agent': secrets['private_key']='';secrets['certificate']=''
            identifier=uuid.uuid4().hex
            path=self.directory/(identifier+'.json')
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as out: out.write(canonical(secrets));out.flush();os.fsync(out.fileno())
            db.execute('INSERT INTO hpc_profiles VALUES (?,?,?,?)',(revision+1,json.dumps(profile),identifier,datetime.now(timezone.utc).isoformat()))
        return self.status()

    def ssh_arguments(self,revision):
        """Trusted adapter entry: exact stored revision, never the mutable default."""
        row=self._row(revision)
        if row is None: raise TaskError('没有该版本的 HPC 连接。')
        profile=validate(json.loads(row['configuration']))
        secrets=json.loads(read_regular(self.directory/(row['credential_id']+'.json'),100000,private=True))
        def materialize(name,content):
            path=self.directory/(row['credential_id']+'.'+name)
            raw=content.encode()
            if path.exists():
                if read_regular(path,100000,private=True)!=raw: raise TaskError('认证文件发生变化，请重新保存连接。')
            else:
                fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,'wb') as out: out.write(raw)
            return str(path)
        argv=['ssh','-F','/dev/null','-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
              '-o','ConnectTimeout=10','-o','ConnectionAttempts=1','-o','ClearAllForwardings=yes',
              '-o','ForwardAgent=no','-o','ForwardX11=no','-o','PermitLocalCommand=no','-o','ControlMaster=no','-o','ControlPath=none']
        if secrets['known_hosts'].strip(): argv+=['-o','UserKnownHostsFile='+materialize('known_hosts',secrets['known_hosts'])]
        if profile['authentication']=='private_key':
            argv+=['-o','IdentitiesOnly=yes','-o','IdentityAgent=none','-i',materialize('key',secrets['private_key'])]
            if secrets['certificate'].strip(): argv+=['-o','CertificateFile='+materialize('cert.pub',secrets['certificate'])]
        argv+=['-p',str(profile['port']),'-l',profile['username'],profile['host']]
        return argv

    def check(self,revision):
        ok=self.probe(self.ssh_arguments(revision)+['true'])
        with self.tasks.transaction() as db:
            db.execute('INSERT INTO hpc_connection_checks(revision,connected,at) VALUES (?,?,?)',(revision,bool(ok),datetime.now(timezone.utc).isoformat()))
        return dict(revision=revision,connected=bool(ok),message=('SSH 连接成功。尚未验证 Slurm、工作目录与计算环境。' if ok else '未能连接。请核对地址、认证和主机公钥；若集群需要 VPN，请先连接。未提交计算。'))
