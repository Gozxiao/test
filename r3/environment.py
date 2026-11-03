"""Explicit child environment contract. Never forward the webhook service env."""
import os
from pathlib import Path

PUBLIC_SITE = frozenset(('VITE_PINGVIN_SHARE_URL', 'VITE_STIRLING_PDF_URL',
    'VITE_NARRALUME_URL', 'VITE_DIGITAL_LIFE_URL', 'WEB_HOST', 'WEB_PORT',
    'CORS_ORIGINS', 'ADMIN_LOGIN_MAX_FAILS', 'ADMIN_LOGIN_LOCK_MINUTES'))


def clean_environment(source=None, site=False):
    source = os.environ if source is None else source
    if os.name == 'posix':
        result = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                  'HOME': '/root', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8'}
    else:  # Offline Windows checks only; production entry still refuses Windows.
        result = {k: source[k] for k in ('SystemRoot', 'SYSTEMROOT', 'WINDIR') if k in source}
        result.update(PATH=os.defpath)
    if site:
        result.update({k: source[k] for k in PUBLIC_SITE if k in source})
    return result


def require_install(install, caller):
    if os.name != 'posix' or os.getuid() != 0:
        raise PermissionError('external Linux root installation required')
    caller = Path(caller).absolute()
    if caller.parent != install or caller.resolve() != caller:
        raise PermissionError('fixed non-symlink entry required')
    paths = (install, *install.parents, *(install / name for name in ('controller.py', 'executor.py', 'environment.py')))
    for path in paths:
        st = path.stat()
        if path.is_symlink() or st.st_uid != 0 or st.st_mode & 0o022:
            raise PermissionError('root-owned non-writable installation required')
