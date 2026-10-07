"""Fixed HPC potential metadata and bindings; model bytes never enter local storage.

The administrator registers/inspects resources on HPC first. A pinned staging
helper returns only immutable metadata. Registration is not a physics validation.
"""
import base64
import json
from pathlib import Path
import re
import shlex
import uuid

from .manifest import canonical, private_directory, read_file, root_descriptor, sha256
from .potentials import PotentialBinding, PotentialError, _digest, _metadata, _name, _roles, MAX_FILE
from .staging import BOOTSTRAP
from .slurm_read import _capture, _write_new

LIMIT = 1_000_000


def validate_entry(entry):
    if not isinstance(entry, dict) or set(entry) != {'pin', 'record'}:
        raise PotentialError('Only fixed potential metadata may enter the application')
    _digest(entry['pin'])
    record = entry['record']
    if (not isinstance(record, dict)
            or set(record) != {'schema_version', 'status', 'metadata', 'files', 'inspection'}
            or type(record['schema_version']) is not int or record['schema_version'] != 1
            or record['status'] != 'collected' or sha256(canonical(record)) != entry['pin']):
        raise PotentialError('Remote potential metadata identity differs')
    meta = _metadata(record['metadata'])
    if not isinstance(record['files'], dict) or set(record['files']) != _roles(meta):
        raise PotentialError('Missing complete format-specific potential files and license')
    names = set()
    for item in record['files'].values():
        if (not isinstance(item, dict) or set(item) != {'name', 'size', 'sha256'}
                or type(item['size']) is not int or not 1 <= item['size'] <= MAX_FILE):
            raise PotentialError('Invalid remote potential file')
        name = _name(item['name']); _digest(item['sha256'])
        if name in names:
            raise PotentialError('Repeated remote potential basename')
        names.add(name)
    inspection = record['inspection']
    if (not isinstance(inspection, dict) or not isinstance(inspection.get('blockers'), list)
            or any(not isinstance(x, str) for x in inspection['blockers'])
            or inspection.get('elements') != meta['elements']):
        raise PotentialError('Invalid registered potential inspection')
    return entry


def validate_view(view):
    if (not isinstance(view, dict) or set(view) != {'schema_version', 'catalog_sha256', 'entries'}
            or type(view['schema_version']) is not int or view['schema_version'] != 1
            or not isinstance(view['entries'], list) or len(view['entries']) > 128):
        raise PotentialError('Invalid remote potential catalog view')
    _digest(view['catalog_sha256'])
    pins = set()
    for entry in view['entries']:
        validate_entry(entry)
        if entry['pin'] in pins:
            raise PotentialError('Repeated remote potential pin')
        pins.add(entry['pin'])
    return view


def fixed_binding(entry, catalog_sha256, *, type_elements, software_sha256):
    """Reconstruct file names and commands from metadata, never model authority."""
    validate_entry(entry); _digest(catalog_sha256); _digest(software_sha256)
    pin, record = entry['pin'], entry['record']
    meta = record['metadata']
    if (not isinstance(type_elements, list) or not 1 <= len(type_elements) <= 118
            or any(not isinstance(element, str) or element not in meta['elements'] for element in type_elements)
            or meta['interaction'] != 'standalone' or record['inspection']['blockers']):
        raise PotentialError('Remote potential binding lacks a complete compatible type mapping')
    prefix = 'potentials/' + pin
    names = {'license': 'LICENSE.txt'}
    if meta['format'] == 'meam':
        names.update(library='library.meam', parameters='model.meam')
        commands = ('pair_style meam', f'pair_coeff * * {prefix}/library.meam '
                    + ' '.join(meta['elements']) + f' {prefix}/model.meam ' + ' '.join(type_elements))
    elif meta['format'] == 'eam/alloy':
        names.update(model='model.eam.alloy')
        commands = ('pair_style eam/alloy', f'pair_coeff * * {prefix}/model.eam.alloy ' + ' '.join(type_elements))
    else:
        names.update(coefficients='model.snapcoeff', parameters='model.snapparam')
        commands = ('pair_style snap', f'pair_coeff * * {prefix}/model.snapcoeff {prefix}/model.snapparam ' + ' '.join(type_elements))
    external = {}
    for role, name in names.items():
        item = record['files'][role]
        path = prefix + '/' + name
        external[path] = dict(path=path, role='potential', size=item['size'], sha256=item['sha256'],
            external_source=dict(kind='potential', catalog_sha256=catalog_sha256, pin=pin, role=role))
    receipt = dict(schema_version=1, potential_sha256=pin, software_sha256=software_sha256,
        atom_type_elements=list(type_elements), units=meta['units'],
        files={path: item['sha256'] for path, item in external.items()}, commands=list(commands),
        checks='static_resource_binding', execution_authorized=False, environment_verified=False,
        scientifically_verified=False, remote_potential=dict(catalog_sha256=catalog_sha256, entry=entry))
    if meta['format'] == 'meam':
        receipt['library_index_elements'] = list(meta['elements'])
        receipt['potential_warnings'] = record['inspection'].get('warnings', [])
    if meta['format'] == 'eam/alloy':
        receipt['model_element_order'] = list(meta['elements'])
        receipt['potential_warnings'] = record['inspection']['warnings']
        receipt['ignored_line_tails'] = record['inspection']['ignored_line_tails']
    return PotentialBinding(pin, {}, commands, receipt, external)


class RemotePotentialCatalog:
    hpc_only = True

    def __init__(self, directory):
        self.directory = private_directory(directory)
        _digest(self.directory.name)
        with root_descriptor(self.directory) as root:
            raw = read_file(root, 'catalog-view.json', LIMIT)
        if sha256(raw) != self.directory.name:
            raise PotentialError('Local remote-metadata cache changed')
        self.view = validate_view(json.loads(raw))

    def read(self, pin):
        _digest(pin)
        # Re-read the pinned metadata so a mutation is rejected, not hidden by cache.
        with root_descriptor(self.directory) as root:
            raw = read_file(root, 'catalog-view.json', LIMIT)
        if sha256(raw) != self.directory.name:
            raise PotentialError('Local remote-metadata cache changed')
        view = validate_view(json.loads(raw))
        entry = next((x for x in view['entries'] if x['pin'] == pin), None)
        if entry is None:
            raise PotentialError('Potential is absent from the fixed HPC metadata view')
        return json.loads(canonical(entry['record'])), {}

    def list_models(self):
        return [dict(pin=x['pin'], record=self.read(x['pin'])[0]) for x in self.view['entries']]

    def binding(self, pin, *, type_elements, software_sha256):
        record, _ = self.read(pin)
        with root_descriptor(self.directory) as root:
            raw = read_file(root, 'catalog-view.json', LIMIT)
        if sha256(raw) != self.directory.name:
            raise PotentialError('Local remote-metadata cache changed')
        return fixed_binding(dict(pin=pin, record=record), validate_view(json.loads(raw))['catalog_sha256'],
            type_elements=type_elements, software_sha256=software_sha256)


class CombinedPotentialCatalog:
    """Preserve existing local pins while adding registered HPC-only resources."""
    def __init__(self, local, remote):
        self.local, self.remote, self.directory = local, remote, local.directory
        self.remote_pins = frozenset(item['pin'] for item in remote.list_models())
        if self.remote_pins & {item['pin'] for item in local.list_models()}:
            raise PotentialError('A potential pin cannot have ambiguous local and HPC sources')

    def is_remote(self, pin):
        return pin in self.remote_pins

    def read(self, pin):
        return (self.remote if self.is_remote(pin) else self.local).read(pin)

    def list_models(self):
        return self.local.list_models() + self.remote.list_models()

    def binding(self, pin, **kwargs):
        if not self.is_remote(pin):
            raise PotentialError('Requested resource is not HPC-only')
        return self.remote.binding(pin, **kwargs)


class PotentialCatalogClient:
    """Read metadata through the same configured/pinned HPC staging endpoint."""
    def __init__(self, stage_client):
        self.client = stage_client

    def list(self):
        e = self.client.endpoint
        remote = [e.python_path, '-I', '-c', BOOTSTRAP, e.helper_path, e.helper_sha256, '--list-potentials']
        command = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                   '-o', 'ConnectTimeout=10', '-o', 'ClearAllForwardings=yes', '-o', 'ForwardAgent=no',
                   '-o', 'ForwardX11=no', '-o', 'PermitLocalCommand=no', e.host_alias, shlex.join(remote)]
        if self.client.transport is not None:
            command = self.client.transport.command(e.host_alias, remote)
        trace = self.client.audit_directory / uuid.uuid4().hex
        trace.mkdir(mode=0o700)
        _write_new(trace / 'intent.json', dict(kind='potential_metadata_read', helper_sha256=e.helper_sha256))
        try:
            result = _capture(command, timeout=self.client.timeout, max_bytes=LIMIT)
        except (OSError, ValueError) as error:
            result = dict(returncode=None, failure=type(error).__name__, stdout='', stderr='')
        _write_new(trace / 'result.json', result)
        if result.get('failure') or result.get('returncode') != 0:
            raise PotentialError('Configured HPC potential metadata could not be verified')
        try:
            view = json.loads(base64.b64decode(result['stdout'], validate=True))
            return validate_view(view)
        except (ValueError, TypeError, KeyError, UnicodeError) as error:
            raise PotentialError('Invalid fixed HPC potential response') from error


def save_metadata_view(view, directory):
    """Administrator cache of metadata only, addressed by its own byte hash."""
    raw = canonical(validate_view(view))
    directory = private_directory(Path(directory) / sha256(raw))
    path = directory / 'catalog-view.json'
    if not path.exists():
        with path.open('xb') as output:
            output.write(raw)
        path.chmod(0o400)
    RemotePotentialCatalog(directory)
    return directory


def register_catalog_version(directory):
    """HPC administrator operation on already inspected PotentialCatalog objects.

    No CLI/model path selection. Original resource bytes stay in this HPC store;
    preserved versions let previously frozen submissions keep their identities.
    """
    from .potentials import PotentialCatalog
    from .geometry_catalog import _locked, _immutable
    catalog = PotentialCatalog(directory)
    with _locked(catalog.directory) as root:
        entries = []
        for item in catalog.list_models():
            entry = validate_entry(item)
            entries.append(dict(**entry, paths={role: entry['pin'] + '/' + rec['name']
                                               for role, rec in entry['record']['files'].items()}))
        raw = canonical(dict(schema_version=1, entries=entries))
        if len(raw) > LIMIT:
            raise PotentialError('HPC potential catalog metadata exceeds its bound')
        versions = private_directory(catalog.directory / 'versions')
        _immutable(versions / (sha256(raw) + '.json'), raw)
        current = catalog.directory / 'catalog.json'
        import os, tempfile
        fd, temp = tempfile.mkstemp(prefix='.catalog-', dir=catalog.directory)
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(raw); output.flush(); os.fsync(output.fileno())
            os.chmod(temp, 0o400); os.replace(temp, current); os.fsync(root)
        finally:
            if os.path.exists(temp): os.unlink(temp)
    return dict(schema_version=1, catalog_sha256=sha256(raw), entries=[{k: x[k] for k in ('pin', 'record')} for x in entries])
