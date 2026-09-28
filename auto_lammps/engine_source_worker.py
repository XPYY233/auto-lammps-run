"""Trusted environment preparation; downloads source, never executes a build.

Build plans are immutable inputs for reviewed, accounted deployment work. A
source archive or help listing is not a deployed or scientifically tested engine.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import tarfile
import time
import urllib.request
import uuid

import hashlib
import stat
import sys
import socket


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def private_directory(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Absolute private directory required')
    for parent in reversed([path, *path.parents]):
        if parent.is_symlink():
            raise ValueError('Symlink directory rejected')
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError('Private owned directory required')
    return path


def _write_new(path, value):
    with path.open('xb') as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(canonical(value)); output.flush(); os.fsync(output.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

MAX_DOWNLOAD = 256*1024*1024
MAX_SOURCE = 1024*1024*1024


class EnginePreparationError(ValueError):
    pass


def requirements_from_models(report, *, release, cores):
    if (not isinstance(release, str) or not re.fullmatch(r'[1-9][0-9]? [A-Z][a-z]{2} 20[0-9]{2}', release)
            or type(cores) is not int or not 1 <= cores <= 8):
        raise EnginePreparationError('Explicit release and bounded core count are required')
    bindings = report.get('bindings', [])
    if not bindings or report.get('state') not in {'finished', 'partial'}:
        raise EnginePreparationError('No collected potential requirements')
    requirements = set()
    for binding in bindings:
        if binding.get('required_package') != 'MEAM' or binding.get('pair_style') != 'meam':
            raise EnginePreparationError('Unsupported engine package requirement')
        requirements.add(('MEAM', 'meam'))
    return dict(release=release, packages=sorted({x[0] for x in requirements}),
                pair_styles=sorted({x[1] for x in requirements}), cores=cores, mpi=cores > 1,
                model_report_sha256=sha256(canonical(report)))


def validate_requirements(value):
    if (not isinstance(value, dict) or set(value) !=
            {'release', 'packages', 'pair_styles', 'cores', 'mpi', 'model_report_sha256'}
            or not isinstance(value['release'], str)
            or not re.fullmatch(r'[1-9][0-9]? [A-Z][a-z]{2} 20[0-9]{2}', value['release'])
            or type(value['cores']) is not int or not 1 <= value['cores'] <= 8
            or type(value['mpi']) is not bool or value['mpi'] != (value['cores'] > 1)
            or value['packages'] != ['MEAM'] or value['pair_styles'] != ['meam']
            or not isinstance(value['model_report_sha256'], str)
            or not re.fullmatch('[a-f0-9]{64}', value['model_report_sha256'])):
        raise EnginePreparationError('Invalid frozen engine requirements')
    return json.loads(canonical(value))


def inspect_help(data):
    if not isinstance(data, bytes) or not 1 <= len(data) <= 500000:
        raise EnginePreparationError('Missing or excessive engine help')
    try:
        text = data.decode('utf-8')
    except UnicodeError as exc:
        raise EnginePreparationError('Invalid engine help encoding') from exc
    versions = re.findall(r'^Large-scale Atomic/Molecular Massively Parallel Simulator - (.+)$', text, re.M)
    package_section = re.search(r'Installed packages:\s*\n(.*?)\nList of individual style', text, re.S)
    pairs = re.search(r'^\* Pair styles:\s*\n(.*?)(?=^\* |\Z)', text, re.S | re.M)
    if len(versions) != 1 or package_section is None or pairs is None:
        raise EnginePreparationError('Incomplete or ambiguous engine help')
    # Exact release string comparison retains patch/update distinctions.
    version = versions[0].strip()
    mpi_lines = re.findall(r'^MPI v[^\n]+', text, re.M)
    if len(mpi_lines) != 1:
        mpi = None
    else:
        mpi = not bool(re.search(r'STUBS|serial', mpi_lines[0], re.I))
    return dict(release=version, packages=sorted(set(package_section[1].split())),
                pair_styles=sorted(set(pairs[1].split())), mpi=mpi, help_sha256=sha256(data))


def assess_engine(requirements, observed):
    requirements = validate_requirements(requirements)
    missing = []
    if observed.get('release') != requirements['release']:
        missing.append('release_mismatch')
    for package in requirements['packages']:
        if package not in observed.get('packages', []):
            missing.append('missing_package:'+package)
    for style in requirements['pair_styles']:
        if style not in observed.get('pair_styles', []):
            missing.append('missing_pair_style:'+style)
    if requirements['mpi'] and observed.get('mpi') is not True:
        missing.append('MPI_unavailable_or_unverified')
    return dict(metadata_matches=not missing, reasons=missing, environment_verified=False,
                scientific_validation=False, execution_authorized=False)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise EnginePreparationError('Unexpected source-download redirect')


def download_source(url, destination, *, maximum):
    """Fixed official host; incomplete downloads remain as evidence."""
    if not re.fullmatch(r'https://codeload\.github\.com/lammps/lammps/tar\.gz/[a-f0-9]{40}', url):
        raise EnginePreparationError('Unsupported source URL')
    if sys.platform != 'linux' or not os.environ.get('SSH_CONNECTION'):
        raise EnginePreparationError('Concrete source downloads require the remote HPC worker')
    opener = urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(url, headers={'User-Agent': 'Auto-LAMMPS-source-preparation'})
    total = 0
    deadline = time.monotonic()+180
    with opener.open(request, timeout=30) as response, destination.open('xb') as output:
        if response.status != 200:
            raise EnginePreparationError('Source download failed')
        while block := response.read(1024*1024):
            if time.monotonic() > deadline:
                raise EnginePreparationError('Source download time limit')
            total += len(block)
            if total > maximum:
                raise EnginePreparationError('Source download budget exceeded')
            output.write(block)
        output.flush()
        os.fsync(output.fileno())
    destination.chmod(0o400)
    return total


def unpack_source(archive, destination, commit, *, maximum=MAX_SOURCE):
    """Regular-file materialization, including bounded in-tree link targets."""
    import posixpath
    root = 'lammps-'+commit
    destination.mkdir(mode=0o700, exist_ok=False)
    total, records, members = 0, [], {}

    def selected(parts):
        return bool(parts) and (parts[0] in {'src', 'cmake', 'lib'}
                               or len(parts) == 1 and parts[0] in {'LICENSE', 'README'})

    with tarfile.open(archive, mode='r:gz') as package:
        for count, entry in enumerate(package, 1):
            if count > 50000:
                raise EnginePreparationError('Source archive entry limit')
            name = PurePosixPath(entry.name)
            if (name.is_absolute() or '..' in name.parts or not name.parts or name.parts[0] != root
                    or '\\' in entry.name or '\x00' in entry.name):
                raise EnginePreparationError('Unsafe source archive path')
            normalized = str(name)
            if normalized in members:
                raise EnginePreparationError('Duplicate source archive member')
            members[normalized] = entry
        for name, entry in members.items():
            parts = PurePosixPath(name).parts[1:]
            if not selected(parts) or entry.isdir():
                continue
            content_entry = entry
            link = None
            if entry.issym():
                link = entry.linkname
                if not link or link.startswith('/') or '\\' in link or '\x00' in link:
                    raise EnginePreparationError('Unsafe source link')
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), link))
                target_parts = PurePosixPath(resolved).parts
                if not target_parts or target_parts[0] != root or not selected(target_parts[1:]):
                    raise EnginePreparationError('Source link leaves selected tree')
                content_entry = members.get(resolved)
                # No link chains, hard links, external targets or special files.
                if content_entry is None or not content_entry.isfile():
                    raise EnginePreparationError('Source link must target a regular archived file')
            if not content_entry.isfile() or content_entry.size > 32*1024*1024:
                raise EnginePreparationError('Unsupported source archive member')
            total += content_entry.size
            if total > maximum:
                raise EnginePreparationError('Unpacked source budget exceeded')
            target = destination.joinpath(*parts)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            content = package.extractfile(content_entry).read(content_entry.size+1)
            if len(content) != content_entry.size:
                raise EnginePreparationError('Truncated source member')
            with target.open('xb') as handle:
                handle.write(content); handle.flush(); os.fsync(handle.fileno())
            target.chmod(0o500 if content_entry.mode & 0o111 else 0o400)
            record = dict(path='/'.join(parts), size=len(content), sha256=sha256(content))
            if link is not None:
                record.update(archive_symlink=link, materialized_from=resolved)
            records.append(record)
    required = {'LICENSE', 'src/version.h', 'src/MEAM/pair_meam.cpp', 'cmake/CMakeLists.txt'}
    if not required <= {r['path'] for r in records}:
        raise EnginePreparationError('Missing required official source files')
    return dict(files=records, bytes=total)


def cmake_plan(requirements, *, source, build, cmake, compiler, mpi_compiler):
    requirements = validate_requirements(requirements)
    for path in (source, build, cmake, compiler, mpi_compiler):
        if not isinstance(path, str) or not Path(path).is_absolute() or '..' in Path(path).parts or '\x00' in path:
            raise EnginePreparationError('Trusted absolute deployment paths required')
    if source == build or Path(source) in Path(build).parents or Path(build) in Path(source).parents:
        raise EnginePreparationError('Source and build directories must be separate')
    if requirements['packages'] != ['MEAM'] or requirements['pair_styles'] != ['meam']:
        raise EnginePreparationError('Unsupported build requirement')
    return dict(configure=[cmake, '-S', source+'/cmake', '-B', build, '-D', 'CMAKE_BUILD_TYPE=Release',
        '-D', 'CMAKE_CXX_COMPILER='+compiler, '-D', 'MPI_CXX_COMPILER='+mpi_compiler,
        '-D', 'BUILD_MPI='+('ON' if requirements['mpi'] else 'OFF'), '-D', 'BUILD_OMP=OFF',
        '-D', 'BUILD_SHARED_LIBS=OFF', '-D', 'PKG_MEAM=ON', '-D', 'WITH_JPEG=OFF',
        '-D', 'WITH_PNG=OFF', '-D', 'WITH_GZIP=OFF', '-D', 'WITH_FFMPEG=OFF'],
        build=[cmake, '--build', build, '--target', 'lmp', '--parallel', str(requirements['cores'])],
        execution_authorized=False, accounting_required=True, reviewed_commit_required=True,
        target_input_allowed=False)


class EnginePreparation:
    def __init__(self, directory, *, fetch=download_source):
        self.root = private_directory(directory)
        self.fetch = fetch

    def prepare_source(self, requirements, *, commit, storage_bytes, identifier=None, context=None):
        requirements = validate_requirements(requirements)
        if (not isinstance(commit, str) or not re.fullmatch('[a-f0-9]{40}', commit)
                or type(storage_bytes) is not int or not 1 <= storage_bytes <= 16*1024**3):
            raise EnginePreparationError('Pinned source and explicit storage budget required')
        identifier = identifier or uuid.uuid4().hex
        if not re.fullmatch('[a-f0-9]{32}', identifier):
            raise EnginePreparationError('Invalid preparation identity')
        folder = self.root/identifier
        folder.mkdir(mode=0o700)
        report = dict(id=identifier, schema_version=1, at=datetime.now(timezone.utc).isoformat(),
            requirements=requirements, commit=commit, state='preparing', storage_bytes=storage_bytes,
            source_url=f'https://codeload.github.com/lammps/lammps/tar.gz/{commit}',
            built=False, environment_verified=False, scientific_validation=False, execution_authorized=False)
        report.update(context or {})
        _write_new(folder/'intent.json', report)
        try:
            archive = folder/'source.tar.gz'
            self.fetch(report['source_url'], archive, maximum=min(MAX_DOWNLOAD, storage_bytes//2))
            archive_bytes = archive.stat().st_size
            if archive_bytes > min(MAX_DOWNLOAD, storage_bytes//2):
                raise EnginePreparationError('Source download budget exceeded')
            report['archive_sha256'] = sha256(archive.read_bytes())
            report['archive_bytes'] = archive_bytes
            inventory = unpack_source(archive, folder/'source', commit,
                                      maximum=min(MAX_SOURCE, storage_bytes-archive_bytes-1024*1024))
            version = (folder/'source/src/version.h').read_text()
            if not re.search(r'^#define LAMMPS_VERSION "'+re.escape(requirements['release'])+r'"$', version, re.M):
                raise EnginePreparationError('Requested release does not match pinned source')
            _write_new(folder/'inventory.json', inventory)
            report.update(state='source_ready', source_bytes=inventory['bytes'], files=len(inventory['files']),
                          inventory_sha256=sha256(canonical(inventory)))
        except (OSError, ValueError, tarfile.TarError) as exc:
            report.update(state='failed', failure=str(exc) if isinstance(exc, EnginePreparationError) else type(exc).__name__)
        _write_new(folder/'result.json', report)
        return report



def validate_request(request):
    if not isinstance(request, dict) or set(request) != {'id', 'directory', 'requirements', 'commit', 'storage_bytes', 'worker_sha256'}:
        raise EnginePreparationError('Invalid remote preparation request')
    validate_requirements(request['requirements'])
    if (not isinstance(request['id'], str) or not re.fullmatch('[a-f0-9]{32}', request['id'])
            or not isinstance(request['commit'], str) or not re.fullmatch('[a-f0-9]{40}', request['commit'])
            or not isinstance(request['worker_sha256'], str) or not re.fullmatch('[a-f0-9]{64}', request['worker_sha256'])
            or type(request['storage_bytes']) is not int or not 2*1024**2 <= request['storage_bytes'] <= 16*1024**3
            or not isinstance(request['directory'], str) or not Path(request['directory']).is_absolute()
            or '..' in Path(request['directory']).parts or len(request['directory']) > 2048
            or any(ord(c) < 32 for c in request['directory'])):
        raise EnginePreparationError('Invalid remote preparation policy')
    return request


def remote_main(request, worker_source):
    validate_request(request)
    if sys.platform != 'linux' or not os.environ.get('SSH_CONNECTION'):
        raise EnginePreparationError('Remote HPC SSH session required')
    if sha256(worker_source.encode()) != request['worker_sha256']:
        raise EnginePreparationError('Worker identity mismatch')
    root = private_directory(request['directory'])
    identifier = request['id']
    lock = root/(identifier+'.lock.json')
    request_hash = sha256(canonical(request))
    context = dict(location='hpc', remote_directory=str(root/identifier),
                   hostname=socket.gethostname(), worker_sha256=request['worker_sha256'],
                   request_sha256=request_hash)
    unknown = dict(id=identifier, schema_version=1, state='unknown',
                   requirements=request['requirements'], commit=request['commit'],
                   storage_bytes=request['storage_bytes'], built=False, environment_verified=False,
                   scientific_validation=False, execution_authorized=False, **context)
    try:
        _write_new(lock, request)
    except FileExistsError:
        # A prior invocation may still be alive: observe only, never restart.
        if lock.is_symlink() or lock.stat().st_size > 20000 or json.loads(lock.read_text()) != request:
            raise EnginePreparationError('Operation identity already bound to another request')
        result = root/identifier/'result.json'
        if not result.exists():
            return unknown
        if result.is_symlink() or result.stat().st_size > 20000:
            raise EnginePreparationError('Invalid saved result')
        report = json.loads(result.read_text())
        if report.get('request_sha256') != request_hash:
            raise EnginePreparationError('Saved result binding mismatch')
        return report
    with (root/(identifier+'.worker.py')).open('xb') as output:
        os.fchmod(output.fileno(), 0o400)
        output.write(worker_source.encode()); output.flush(); os.fsync(output.fileno())
    return EnginePreparation(root).prepare_source(request['requirements'], commit=request['commit'],
        storage_bytes=request['storage_bytes'], identifier=identifier, context=context)
