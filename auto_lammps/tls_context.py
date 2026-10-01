"""TLS trust for the fixed model endpoints.

Some Python installations on this machine have no configured trust store
(``ssl.get_default_verify_paths()`` returns no cafile and no capath), so every
outbound HTTPS request fails verification while the system tools succeed. The
resolver below picks a bundle that actually exists, prefers an explicit override,
never disables verification, and reports a distinct error when nothing usable is
available. Disabling verification, or shipping a private root inside the public
tree, is deliberately not an option here.
"""
import os
import ssl
from pathlib import Path

SYSTEM_BUNDLES = (
    '/etc/ssl/cert.pem',                      # macOS system roots
    '/etc/ssl/certs/ca-certificates.crt',     # Debian/Ubuntu
    '/etc/pki/tls/certs/ca-bundle.crt',       # RHEL/Fedora
)


def ca_bundle():
    """First existing bundle among explicit overrides, system roots, certifi."""
    candidates = [os.environ.get('SSL_CERT_FILE'), os.environ.get('REQUESTS_CA_BUNDLE')]
    candidates.extend(SYSTEM_BUNDLES)
    try:
        import certifi
        candidates.append(certifi.where())
    except Exception:
        pass
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return None


def ssl_context():
    """Verifying context for fixed-host requests; raises when no bundle exists."""
    bundle = ca_bundle()
    if bundle is None:
        raise OSError('no_ca_bundle_available')
    return ssl.create_default_context(cafile=bundle)
