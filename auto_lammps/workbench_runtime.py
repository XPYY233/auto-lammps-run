"""Assemble a separately installed workbench in this project's private store.

Only the local controller reads this configuration. It is not a browser import,
does not register PDFs or load provider credentials, and never opens the other
application's database, settings, encryption authority or Keychain.
"""
import importlib
import json
import os
import stat
from dataclasses import replace

from .manifest import canonical, sha256
from .runtime_launcher import ExecutionDenied, absolute, directory, read_regular
from .tasks import TaskError
from .workbench_bridge import (WorkbenchExtractionBridge, WorkbenchPaperBinding, WorkbenchRecoveryBinding,
    _digest, installed_workbench_runtime)


class WorkbenchRuntimeError(TaskError):
    code = 'workbench_private_runtime_unavailable'


def _denied():
    return WorkbenchRuntimeError('本项目的文献工作台依赖、私有配置或持久存储未接好；未开始提取。')


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise _denied()
        value[key] = item
    return value


def _json(data):
    return json.loads(data, object_pairs_hook=_unique_object,
        parse_constant=lambda value: (_ for _ in ()).throw(_denied()))


def _private_root(path):
    root = absolute(path)
    fd = directory(root, private=True)
    os.close(fd)
    if any((parent / '.git').exists() for parent in (root, *root.parents)):
        raise _denied()
    return root


def _write_once(path, content):
    parent = directory(path.parent, private=True)
    try:
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, 'wb', closefd=False) as output:
                output.write(content); output.flush(); os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(parent)
    finally:
        os.close(parent)


def _existing_private_file(path):
    """Validate storage metadata without reading a potentially large database."""
    if not path.exists() and not path.is_symlink():
        return False
    parent = directory(path.parent, private=True)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.getuid() or info.st_mode & 0o077):
                raise _denied()
        finally:
            os.close(fd)
    finally:
        os.close(parent)
    return True


class _ProjectCheckpointSealer:
    """Authenticated encryption with this project's own persistent key/domain."""
    _MAGIC = b'ALWB1'
    _DOMAIN = b'auto-lammps/literature-workbench/v1\0'

    def __init__(self, key, cipher_type):
        self._cipher = cipher_type(key)

    def seal(self, plaintext, *, associated_data):
        nonce = os.urandom(12)
        return self._MAGIC + nonce + self._cipher.encrypt(nonce, plaintext,
            self._DOMAIN + associated_data)

    def open(self, ciphertext, *, associated_data):
        if not isinstance(ciphertext, bytes) or not ciphertext.startswith(self._MAGIC):
            raise _denied()
        start = len(self._MAGIC)
        return self._cipher.decrypt(ciphertext[start:start+12], ciphertext[start+12:],
            self._DOMAIN + associated_data)



class _ReissueCheckpointService:
    """Route explicitly authorized new native identities; never alter old stores."""
    def __init__(self, service, *, root, sealer, store_type, service_type):
        self.original, self.root, self.sealer = service, root, sealer
        self.store_type, self.service_type = store_type, service_type

    def reissue_store(self, request_id):
        from .tasks import task_id
        task_id(request_id)
        base = self.root/'explicit-reissues'; base.mkdir(mode=0o700, exist_ok=True)
        _private_root(str(base))
        folder = base/request_id; folder.mkdir(mode=0o700, exist_ok=True)
        _private_root(str(folder))
        return self.store_type(data_root=folder, sealer=self.sealer)

    def register(self, native_id, request_id, authorization_sha256):
        record = canonical(dict(version=1, native_task_id=native_id,
            request_id=request_id, authorization_sha256=authorization_sha256))
        routes = self.root/'explicit-reissue-routes'; routes.mkdir(mode=0o700, exist_ok=True)
        _private_root(str(routes))
        path = routes/(sha256(native_id.encode())+'.json')
        try: _write_once(path, record)
        except FileExistsError:
            if read_regular(path, 2048, private=True) != record: raise _denied()

    def _service(self, native_id):
        path = self.root/'explicit-reissue-routes'/(sha256(native_id.encode())+'.json')
        if not path.exists() and not path.is_symlink(): return self.original
        route = _json(read_regular(path, 2048, private=True))
        if (set(route) != {'version','native_task_id','request_id','authorization_sha256'}
                or route['version'] != 1 or route['native_task_id'] != native_id): raise _denied()
        # Reopening an existing per-authorization store restores the native CAS
        # and cancellation semantics; the browser cannot choose this directory.
        return self.service_type(store=self.reissue_store(route['request_id']))

    def __getattr__(self, name):
        original = getattr(self.original, name)
        if not callable(original): return original
        def routed(*args, **kwargs):
            value = args[0] if args else kwargs.get('task_id', kwargs.get('manifest'))
            if hasattr(value, 'manifest'): value = value.manifest
            if hasattr(value, 'task_id'): value = value.task_id
            service = self._service(value) if isinstance(value, str) else self.original
            if (service is not self.original and name in {'plan_call','begin_call','complete_call',
                    'mark_call_outcome_unknown','advance_stage','complete_task','release'}
                    and isinstance(value,str) and 'expected_revision' in kwargs and 'owner_id' in kwargs):
                # Renew at native tool boundaries, never inside the provider
                # transport and never after expiry. The native operation then
                # consumes the new CAS revision and returns it to its caller.
                import time
                from auto_research.evidence.literature_task_checkpoint import (
                    MAX_LEASE_SECONDS, checkpoint_owner_digest)
                current=service.load(value); now=int(time.time())
                if (current.revision == kwargs['expected_revision']
                        and current.state == 'running'
                        and current.lease_owner_digest == checkpoint_owner_digest(kwargs['owner_id'])
                        and current.lease_expires_at is not None
                        and now < current.lease_expires_at <= now+MAX_LEASE_SECONDS//2
                        and now < current.manifest.expires_at
                        and not service.cancellation_requested(value)):
                    renewed=service.acquire(value,expected_revision=current.revision,
                        owner_id=kwargs['owner_id'],lease_seconds=MAX_LEASE_SECONDS)
                    kwargs={**kwargs,'expected_revision':renewed.revision}
            return getattr(service, name)(*args, **kwargs)
        return routed


def assemble_private_workbench(papers, config_path):
    """Return ``(bridge, {paper_id: trusted_binding})`` for a private JSON config.

    Exact schema: version=1, private_root=existing absolute owner-only directory,
    session_id=stable opaque identity, bindings=[{paper_id,workbench_paper_id,
    pdf_sha256}], optional recoveries=[WorkbenchRecoveryBinding fields]. The
    latter is controller-only evidence for a legacy job or reviewed dependency
    repair, never a browser-supplied token. An empty binding list permits controller-only PDF registration
    into ``bridge.runtime.database`` before saving actual mappings. Registered
    title/DOI/PDF SHA are checked by the bridge for each human task operation.
    Optional ``completed_native_capability_allowlist`` contains at most eight
    reviewed historical capability digests for reading authenticated completed
    publications after an operator dependency repair. It never permits a
    pending/unknown checkpoint to resume under a different native capability.

    Fixed files under private_root: evidence.sqlite, seal.key, owner.json,
    snapshots/, checkpoints/. Existing stores need the original key and owner
    binding; missing or replaced keys never silently create a fresh authority.
    Install ``auto_research`` and its PDF dependencies in the service runtime.
    No dependency path or provider credential is taken from this configuration.
    """
    try:
        config = _json(read_regular(absolute(str(config_path)), 65536, private=True))
        required = {'version', 'private_root', 'session_id', 'bindings'}
        optional = {'recoveries', 'completed_native_capability_allowlist'}
        if (not isinstance(config, dict) or not required <= set(config)
                or set(config) - required - optional
                or type(config['version']) is not int or config['version'] != 1
                or not isinstance(config['private_root'], str)
                or not isinstance(config['session_id'], str)
                or not 1 <= len(config['session_id']) <= 256
                or any(ord(c) < 33 or ord(c) > 126 for c in config['session_id'])
                or not isinstance(config['bindings'], list) or len(config['bindings']) > 1000):
            raise _denied()
        completed_capabilities = config.get('completed_native_capability_allowlist', [])
        if (not isinstance(completed_capabilities, list) or len(completed_capabilities) > 8
                or any(not isinstance(value, str) for value in completed_capabilities)
                or len(set(completed_capabilities)) != len(completed_capabilities)):
            raise _denied()
        for value in completed_capabilities:
            _digest(value)
        recoveries = config.get('recoveries', [])
        if not isinstance(recoveries, list) or len(recoveries) > 1000:
            raise _denied()
        recoveries = [WorkbenchRecoveryBinding(**item) for item in recoveries]
        if (len({r.request_id for r in recoveries}) != len(recoveries)
                or len({r.job_token for r in recoveries}) != len(recoveries)):
            raise _denied()
        bindings = {}
        for item in config['bindings']:
            if (not isinstance(item, dict) or set(item) !=
                    {'paper_id', 'workbench_paper_id', 'pdf_sha256'}):
                raise _denied()
            binding = WorkbenchPaperBinding(**item)
            if binding.paper_id in bindings:
                raise _denied()
            bindings[binding.paper_id] = binding
        if len({binding.workbench_paper_id for binding in bindings.values()}) != len(bindings):
            raise _denied()
        root = _private_root(config['private_root'])
        # Load dependencies before creating storage or its sealing authority.
        def load(name):
            return importlib.import_module('auto_research.' + name)
        database_type = load('evidence.db').EvidenceDB
        blob_type = load('evidence.literature_snapshot_blob').SealedImmutablePDFBlobStore
        checkpoint_store = load('evidence.literature_task_checkpoint_store')
        store_type = checkpoint_store.SealedSQLiteLiteratureCheckpointStore
        service_type = load('evidence.literature_task_checkpoint_service').LiteratureTaskCheckpointService
        checkpoint_type = load('evidence.literature_checkpoint_runtime').LiteratureCheckpointRuntime
        cipher_type = importlib.import_module('cryptography.hazmat.primitives.ciphers.aead').AESGCM
        owner_path, key_path = root/'owner.json', root/'seal.key'
        identity = {'version': 1, 'owner': 'auto-lammps-literature-workbench',
            'task_store_sha256': sha256(str(papers.tasks.path).encode()),
            'session_sha256': sha256(config['session_id'].encode())}
        if owner_path.exists() or owner_path.is_symlink():
            owner = _json(read_regular(owner_path, 2048, private=True))
            key = read_regular(key_path, 32, private=True)
            if len(key) != 32 or owner != {**identity, 'seal_key_sha256': sha256(key)}:
                raise _denied()
        else:
            if any((root/name).exists() or (root/name).is_symlink()
                    for name in ('seal.key', 'evidence.sqlite', 'snapshots', 'checkpoints')):
                raise _denied()
            key = os.urandom(32)
            _write_once(key_path, key)
            _write_once(owner_path, canonical({**identity, 'seal_key_sha256': sha256(key)}))
        sealer = _ProjectCheckpointSealer(key, cipher_type)
        for name in ('snapshots', 'checkpoints'):
            child = root/name
            child.mkdir(mode=0o700, exist_ok=True)
            _private_root(str(child))
        database_path = root/'evidence.sqlite'
        if not _existing_private_file(database_path):
            _write_once(database_path, b'')
        for location in (database_path, root/'checkpoints'/checkpoint_store.DB_FILENAME):
            for suffix in ('', '-journal', '-wal', '-shm'):
                _existing_private_file(location.with_name(location.name+suffix))
        database = database_type(database_path)
        database.init()
        blobs = blob_type(data_root=root/'snapshots', sealer=sealer)
        store = store_type(data_root=root/'checkpoints', sealer=sealer)
        routed = _ReissueCheckpointService(service_type(store=store), root=root, sealer=sealer,
            store_type=store_type, service_type=service_type)
        checkpoints = checkpoint_type(routed)
        runtime = installed_workbench_runtime(database=database, snapshot_blobs=blobs,
            checkpoint_runtime=checkpoints, session_id=config['session_id'],
            explicit_reissue_store=routed)
        runtime = replace(runtime, pdf_intake_root=root/'imports',
            completed_native_capability_allowlist=tuple(completed_capabilities))
        return WorkbenchExtractionBridge(papers, runtime, recoveries=recoveries), bindings
    except WorkbenchRuntimeError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError,
            ExecutionDenied, TaskError):
        raise _denied() from None
