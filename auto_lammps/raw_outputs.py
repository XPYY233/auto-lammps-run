"""Operator downloads of verified, byte-preserved output files, never inputs.

Manual publication is controller-only; accounted collections are read directly.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid
from datetime import datetime, timezone

from .candidate_jobs import CandidateHistory
from .manifest import canonical, sha256
from .runtime_launcher import directory, read_regular
from .tasks import TaskError


class RawOutputs:
    def __init__(self,tasks,papers,*,collections=None,ledger=None):
        if ledger is not None and papers.ledger is not None and ledger.path!=papers.ledger.path:
            raise TaskError('下载与任务必须使用同一计算账本。')
        self.tasks,self.papers,self.ledger=tasks,papers,ledger or papers.ledger
        self.collections=None
        if collections is not None:
            from .results import existing_private_directory
            if self.ledger is None:raise TaskError('结果回收目录需要对应的计算账本。')
            self.collections=existing_private_directory(collections)
        with tasks.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS raw_output_files (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, request_id TEXT NOT NULL, name TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL, receipt_path TEXT NOT NULL, receipt_sha256 TEXT NOT NULL, at TEXT NOT NULL, UNIQUE(task_id,request_id,name))')
            for action in ('UPDATE','DELETE'):
                db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_raw_output_files_{action} BEFORE {action} ON raw_output_files BEGIN SELECT RAISE(ABORT, 'immutable raw output history'); END")

    def _request(self,task,request):
        self.tasks.get(task)
        if self.ledger is None: raise TaskError('计算账本尚未接入。')
        row=self.ledger.get(request)
        with self.tasks.transaction() as db:
            linked=db.execute('SELECT 1 FROM paper_evaluations WHERE task_id=? AND evaluation=?',(task,row['evaluation'])).fetchone()
        if not linked:
            candidate=CandidateHistory(self.tasks).get(task)
            if not candidate or candidate['state']!='prepared' or candidate['result']['snapshot_sha256']!=row['manifest_sha256']:
                raise TaskError('文件不属于这项任务。')
            if sha256(self.tasks.export(task))!=candidate['condition_sha256']: raise TaskError('任务条件已改变。')
            if row['evaluation'] not in {g['id'] for g in self.ledger.product_results(row['manifest_sha256'])}:
                raise TaskError('文件不属于当前研究计算。')
        if row['state'] not in {'completed','failed','cancelled','timeout'} or not row['accounted']:
            raise TaskError('计算尚未结束并完成核算，原始文件暂未发布。')
        return row

    def _proof(self,row,path,receipt_hash,name,size,digest):
        data=read_regular(Path(path),262144,private=True)
        if sha256(data)!=receipt_hash: raise TaskError('原始文件来源记录发生变化。')
        proof=json.loads(data)
        if any(proof.get(k)!=row[k] for k in ('job_id','manifest_sha256')) or proof.get('request_id')!=row['id']:
            raise TaskError('原始文件的计算身份不匹配。')
        matches=[v for v in proof.get('files',[]) if v.get('name')==name]
        if len(matches)!=1 or matches[0].get('kind')!='output' or matches[0].get('size')!=size or matches[0].get('sha256')!=digest:
            raise TaskError('文件未列入核验后的原始输出清单。')

    @staticmethod
    def _open(path):
        path=Path(path).absolute()
        if any((p/'.git').exists() for p in (path.parent,*path.parents)):
            raise TaskError('研究输出须保存在代码仓库之外。')
        parent=directory(path.parent,private=True)
        try: fd=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        finally: os.close(parent)
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.getuid() or info.st_mode&0o077:
            os.close(fd);raise TaskError('原始文件类型或权限未通过核验。')
        return os.fdopen(fd,'rb')

    @staticmethod
    def _hash(stream):
        before=os.fstat(stream.fileno());digest=hashlib.sha256()
        for block in iter(lambda:stream.read(1024*1024),b''): digest.update(block)
        after=os.fstat(stream.fileno())
        if (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):
            raise TaskError('原始文件正在改变，请稍后重试。')
        stream.seek(0)
        return before.st_size,digest.hexdigest()

    def publish(self,task,request,name,path,receipt):
        """Controller-only: verify retained transfer receipt and original bytes."""
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}',name): raise TaskError('原始文件名不正确。')
        row=self._request(task,request)
        with self._open(path) as stream: size,digest=self._hash(stream)
        receipt=Path(receipt).absolute();receipt_hash=sha256(read_regular(receipt,262144,private=True))
        self._proof(row,receipt,receipt_hash,name,size,digest)
        with self.tasks.transaction() as db:
            prior=db.execute('SELECT * FROM raw_output_files WHERE task_id=? AND request_id=? AND name=?',(task,request,name)).fetchone()
            if prior:
                if prior['sha256']!=digest or prior['size']!=size: raise TaskError('同名原始输出已有不同内容，保留旧记录。')
                return prior['id']
            identifier=uuid.uuid4().hex
            db.execute('INSERT INTO raw_output_files VALUES (?,?,?,?,?,?,?,?,?,?)',(identifier,task,request,name,str(Path(path).absolute()),size,digest,str(receipt),receipt_hash,datetime.now(timezone.utc).isoformat()))
        return identifier

    def _collected_files(self,task):
        """Read accounted collection receipts only; never fetch, copy or publish."""
        if self.collections is None:return []
        self.tasks.get(task)
        requests=set()
        with self.tasks.transaction() as db:
            links=db.execute('SELECT evaluation FROM paper_evaluations WHERE task_id=?',(task,)).fetchall()
        for link in links:
            requests.update(r['id'] for r in self.ledger.evaluation_snapshot(link['evaluation'])['requests'])
        candidate=CandidateHistory(self.tasks).get(task)
        if candidate and candidate['state']=='prepared':
            if sha256(self.tasks.export(task))!=candidate['condition_sha256']:
                raise TaskError('任务条件已改变。')
            digest=candidate['result']['snapshot_sha256']
            for group in self.ledger.product_results(digest):
                requests.update(r['id'] for r in group['requests'] if r['manifest_sha256']==digest)
        if len(requests)>256:raise TaskError('原始输出历史过长，需要分页。')
        files=[]
        from .outputs import validate_header
        for request in sorted(requests):
            row=self.ledger.get(request)
            if row['state'] not in {'completed','failed','cancelled','timeout'} or not row['accounted']:
                continue
            self._request(task,request)
            events=self.ledger.events(request)
            receipts=[json.loads(e['payload']) for e in events if e['kind']=='output_fetch_finished']
            saved=next((r for r in reversed(receipts) if r['collected']),None)
            if saved is None:continue
            ticket=saved['ticket']
            if not isinstance(ticket,str) or not re.fullmatch('[a-f0-9]{32}',ticket):
                raise TaskError('回收记录标识无效。')
            folder=self.collections/ticket
            raw=read_regular(folder/'receipt.json',262144,private=True)
            if sha256(raw)!=saved['evidence_sha256']:raise TaskError('回收记录摘要不符。')
            receipt=json.loads(raw);context=receipt['context']
            reservations=[json.loads(e['payload']) for e in events if e['kind']=='output_fetch_started']
            if (receipt['state']!='collected' or context not in reservations or context['ticket']!=ticket
                    or context['request_id']!=request or context['job_id']!=row['job_id']
                    or context['manifest_sha256']!=row['manifest_sha256'] or context['scheduler_state']!=row['state']):
                raise TaskError('回收记录与当前计算不匹配。')
            header=validate_header(receipt['header'],context)
            for item in header['files']:
                # Control receipts and staging/authorization data are never downloads.
                if not item['path'].startswith('output/'):continue
                name=item['path'][7:]
                identifier=sha256(canonical(dict(kind='collected-output',task=task,request=request,
                    receipt=saved['evidence_sha256'],path=item['path'])))
                files.append(dict(id=identifier,name=name,size=item['size'],sha256=item['sha256'],
                    request_id=request,job_id=row['job_id'],state=row['state'],source='accounted_collection',
                    _path=folder/'payload'/item['path']))
        return files

    def listing(self,task):
        self.tasks.get(task)
        with self.tasks.transaction() as db: files=db.execute('SELECT * FROM raw_output_files WHERE task_id=? ORDER BY at,name',(task,)).fetchall()
        result=[]
        for f in files:
            row=self._request(task,f['request_id'])
            self._proof(row,f['receipt_path'],f['receipt_sha256'],f['name'],f['size'],f['sha256'])
            result.append({k:f[k] for k in ('id','name','size','sha256','request_id')} | {'job_id':row['job_id'],'state':row['state']})
        registered={(f['request_id'],f['name']):f for f in result}
        for item in self._collected_files(task):
            old=registered.get((item['request_id'],item['name']))
            if old:
                if (old['size'],old['sha256'])!=(item['size'],item['sha256']):
                    raise TaskError('同名输出的来源记录存在矛盾。')
                continue
            result.append({k:v for k,v in item.items() if k!='_path'})
        return {'files':result,'message':'原始字节保留，下载前核对文件摘要。' if result else '原始输出尚未回收发布；分析图表不代替原始数据。'}

    def download(self,task,identifier):
        if re.fullmatch(r'[a-f0-9]{64}',identifier):
            matches=[f for f in self._collected_files(task) if f['id']==identifier]
            if len(matches)!=1:raise TaskError('这项任务没有对应的回收文件。')
            f=matches[0];stream=self._open(f['_path'])
            try:
                if self._hash(stream)!=(f['size'],f['sha256']):raise TaskError('原始文件摘要不符，下载已停止。')
            except BaseException:stream.close();raise
            return f['name'],f['size'],stream
        if not re.fullmatch(r'[a-f0-9]{32}',identifier): raise TaskError('文件标识无效。')
        with self.tasks.transaction() as db: f=db.execute('SELECT * FROM raw_output_files WHERE id=? AND task_id=?',(identifier,task)).fetchone()
        if not f: raise TaskError('这项任务没有对应的原始文件。')
        row=self._request(task,f['request_id'])
        self._proof(row,f['receipt_path'],f['receipt_sha256'],f['name'],f['size'],f['sha256'])
        stream=self._open(f['path'])
        try:
            if self._hash(stream)!=(f['size'],f['sha256']): raise TaskError('原始文件摘要不符，下载已停止。')
        except BaseException: stream.close();raise
        return f['name'],f['size'],stream
