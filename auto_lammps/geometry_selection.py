"""Pure validation of a trusted, immutable initial-geometry selection.

Only a resource-service resolution may supply this object. User/model-supplied
paths, coordinates and arbitrary text are not resource selectors. Validation is
metadata-only and never certifies physical stability or scientific suitability.
"""
import json

from .geometry_catalog import GeometryCatalogError, _hash, validate_entry
from .manifest import canonical


def validate_initial_geometry(value):
    """Return detached, allowlisted metadata bound to one catalog and input pin."""
    if not isinstance(value, dict) or set(value) != {'catalog_sha256', 'entry'}:
        raise GeometryCatalogError('Initial geometry requires only an immutable catalog and entry')
    _hash(value['catalog_sha256'])
    validate_entry(value['entry'])
    # Detach nested metadata from caller-owned dictionaries before persistence.
    # The catalog validator rejects free text, coordinates and scientific claims.
    return json.loads(canonical(value))
