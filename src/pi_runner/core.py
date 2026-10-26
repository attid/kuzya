"""Filesystem policy and configuration. No credentials are persisted here."""
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat


class RunnerError(Exception):
    pass


def within(path, parent):
    return path == parent or parent in path.parents


def overlap(a, b):
    return within(a, b) or within(b, a)


def is_network_restricted(val: object) -> bool:
    """Return True if network mode indicates restricted sandbox network.

    Accepts boolean False or string 'restricted' / 'false' (case-insensitive).
    Returns False for boolean True, 'unrestricted', 'true', or unspecified (True by default).
    """
    if isinstance(val, str):
        return val.strip().lower() in ('restricted', 'false')
    return val is False


@dataclass
class Config:
    agent_id: str
    project: Path
    state_dir: Path
    pi_command: list[str]
    runtime_paths: list[Path]
    readonly_paths: list[Path]
    instructions: list[Path]
    tools: list[str]
    network: bool | str
    model: str | None
    provider: str | None
    credential_env: list[str]
    workdir: str
    models_file: Path | None = None
    pi_auth_provider: str | None = None
    project_read_only: bool = False
    skills: list[str] = field(default_factory=list)
    allow_git_commit: bool = False
    knowledge_dir: Path | None = None
    skills_dir: Path | None = None
    backend: dict[str, str] | None = None
    inbox_dir: Path | None = None
    attachments_dir: Path | None = None
    outbox_dir: Path | None = None
    allowed_hosts: list[str] = field(default_factory=list)
    # Runtime-only host path for the restricted-network egress proxy. It is
    # deliberately not loaded from role configuration because it is allocated
    # for one launch and removed when that launch ends.
    egress_proxy_uds: Path | None = None

    @property
    def is_network_restricted(self) -> bool:
        """Convenience property checking if this configuration isolates networking."""
        return is_network_restricted(self.network)

    @classmethod
    def load(cls, path):
        data = json.loads(Path(path).read_text())
        def absolute(value):
            p = Path(value).expanduser()
            if not p.is_absolute():
                raise RunnerError(f'Expected an absolute path: {value}')
            return p.resolve()
        project = absolute(data['project'])
        state = absolute(data['state_dir'])
        if not project.is_dir() or project == Path('/'):
            raise RunnerError('project must be an existing directory other than /')
        if within(Path.home().resolve(), project):
            raise RunnerError('project cannot expose the real HOME or its parent')
        if overlap(project, state):
            raise RunnerError('state_dir must be outside the project')
        runtime = [absolute(p) for p in data.get('runtime_paths', [])]
        readonly = [absolute(p) for p in data.get('readonly_paths', [])]
        for p in runtime + readonly:
            if not p.exists():
                raise RunnerError(f'Missing dependency: {p}')
            # Narrow tool installations under HOME are valid; HOME itself is not.
            if p == Path('/') or within(Path.home().resolve(), p) or overlap(p, project) or overlap(p, state):
                raise RunnerError(f'Overbroad or overlapping dependency: {p}')
            if p in [Path('/etc'), Path('/run'), Path('/var'), Path('/tmp'), Path('/home')] or any(within(p, Path(root)) for root in ('/proc', '/sys', '/dev', '/run')):
                raise RunnerError(f'System data directory cannot be a dependency: {p}')
            if any(part in {'.ssh', '.aws', '.gnupg', '.git'} for part in p.parts):
                raise RunnerError(f'Credential or Git directory cannot be a dependency: {p}')
        command = data.get('pi_command', ['pi'])
        if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
            raise RunnerError('pi_command must be a nonempty array of strings')
        exe = shutil.which(command[0])
        if not exe:
            raise RunnerError(f'Pi executable not found: {command[0]}')
        command = [str(Path(exe).resolve()), *command[1:]]
        for arg in command:
            if arg.startswith('/'):
                p = Path(arg).resolve()
                if not within(p, Path('/usr')) and not any(within(p, r) for r in runtime):
                    raise RunnerError(f'pi_command path needs an explicit runtime_paths entry: {p}')
        workdir = data.get('workdir', '.')
        wd = (project / workdir).resolve()
        if not within(wd, project) or not wd.is_dir():
            raise RunnerError('workdir must resolve to a directory inside project')
        instructions = [absolute(p) for p in data.get('instructions', [])]
        if not all(p.is_file() for p in instructions):
            raise RunnerError('instructions must refer to readable files')
        models_file = absolute(data['models_file']) if data.get('models_file') else None
        if models_file and not models_file.is_file():
            raise RunnerError('models_file must be an existing file')
        knowledge_dir = absolute(data['knowledge_dir']) if data.get('knowledge_dir') else None
        skills_dir = absolute(data['skills_dir']) if data.get('skills_dir') else None
        # Optional explicit media mounts. ``inbox_dir`` replaces the implicit
        # ``<project>/inbox`` scan with a conversation/turn-scoped directory;
        # ``attachments_dir`` exposes earlier same-conversation assets
        # read-only without treating them as current attachments.
        inbox_dir = absolute(data['inbox_dir']) if data.get('inbox_dir') else None
        attachments_dir = absolute(data['attachments_dir']) if data.get('attachments_dir') else None
        outbox_dir = absolute(data['outbox_dir']) if data.get('outbox_dir') else None
        for label, directory in (('knowledge_dir', knowledge_dir), ('skills_dir', skills_dir),
                                 ('inbox_dir', inbox_dir), ('attachments_dir', attachments_dir),
                                 ('outbox_dir', outbox_dir)):
            if directory is not None and not directory.is_dir():
                raise RunnerError(f'{label} must be an existing directory')
        if inbox_dir is not None and (inbox_dir == Path('/') or within(project, inbox_dir)):
            raise RunnerError('inbox_dir must not expose the project or /')
        if attachments_dir is not None and attachments_dir == Path('/'):
            raise RunnerError('attachments_dir must not be /')
        if outbox_dir is not None and (outbox_dir == Path('/') or within(project, outbox_dir)):
            raise RunnerError('outbox_dir must not expose the project or /')
        backend = data.get('backend')
        if backend is not None:
            if not isinstance(backend, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in backend.items()
            ):
                raise RunnerError('backend must be an object of string values')
        # Network mode validation
        raw_network = data.get('network', True)
        if not isinstance(raw_network, (bool, str)):
            raise RunnerError('network must be a boolean or "restricted"')
        if isinstance(raw_network, str):
            clean_net = raw_network.strip().lower()
            if clean_net not in ('restricted', 'false', 'true', 'unrestricted'):
                raise RunnerError(f'Invalid network mode: {raw_network!r}. Expected boolean or "restricted"')
            network: bool | str = raw_network
        else:
            network = bool(raw_network)

        # Allowed hosts validation
        raw_allowed_hosts = data.get('allowed_hosts', [])
        if not isinstance(raw_allowed_hosts, list) or not all(isinstance(h, str) for h in raw_allowed_hosts):
            raise RunnerError('allowed_hosts must be an array of strings')
        allowed_hosts = [h.strip() for h in raw_allowed_hosts if h.strip()]

        return cls(data['agent_id'], project, state, command, runtime, readonly,
                   instructions, data.get('tools', ['read', 'bash', 'edit', 'write']),
                   network, data.get('model'), data.get('provider'),
                   data.get('credential_env', []), str(wd.relative_to(project)), models_file,
                   data.get('pi_auth_provider'), data.get('project_read_only', False),
                   data.get('skills', []), data.get('allow_git_commit', False),
                   knowledge_dir, skills_dir, backend, inbox_dir, attachments_dir, outbox_dir,
                   allowed_hosts)


def sandbox_command(config, home, session, egress_proxy_uds=None):
    bwrap = shutil.which('bwrap')
    if not bwrap:
        raise RunnerError('bubblewrap is required; refusing an unsandboxed launch')
    c = config
    host_uds = egress_proxy_uds
    if host_uds is None:
        host_uds = getattr(c, 'egress_proxy_uds', None)
    host_uds = Path(host_uds) if host_uds is not None else None
    argv = [bwrap, '--die-with-parent', '--new-session', '--unshare-pid',
            '--unshare-ipc', '--unshare-uts', '--unshare-cgroup-try', '--cap-drop', 'ALL',
            '--clearenv', '--ro-bind', '/usr', '/usr']
    if is_network_restricted(c.network):
        argv += ['--unshare-net']
        proxy_url = 'http://127.0.0.1:18777'
        argv += [
            '--setenv', 'HTTP_PROXY', proxy_url,
            '--setenv', 'HTTPS_PROXY', proxy_url,
            '--setenv', 'http_proxy', proxy_url,
            '--setenv', 'https_proxy', proxy_url,
            '--setenv', 'ALL_PROXY', proxy_url,
            '--setenv', 'NODE_OPTIONS', '--use-env-proxy',
        ]
    for name in ('bin', 'sbin', 'lib', 'lib64'):
        p = Path('/') / name
        if p.is_symlink():
            argv += ['--symlink', os.readlink(p), str(p)]
        elif p.exists():
            argv += ['--ro-bind', str(p), str(p)]
    argv += ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
             '--dir', '/etc', '--dir', '/run', '--dir', '/home']
    # Only public OS configuration, never all of /etc.
    for name in ('ld.so.cache', 'ssl/certs', 'ca-certificates', 'resolv.conf', 'hosts', 'nsswitch.conf'):
        p = Path('/etc') / name
        if p.exists():
            argv += ['--ro-bind', str(p.resolve()), str(p)]
    for p in c.runtime_paths + c.readonly_paths:
        argv += ['--ro-bind', str(p), str(p)]
    argv += ['--ro-bind' if c.project_read_only else '--bind', str(c.project), '/workspace', '--bind', str(home), '/home/runner',
             '--bind', str(session), '/session']
    if is_network_restricted(c.network) and host_uds is not None:
        # AF_UNIX paths are limited to 108 bytes. The proxy socket therefore
        # lives on a short host path, and this fixed in-sandbox path is only a
        # bind-mount target. Bubblewrap needs the target to exist before a
        # file bind can replace it.
        mountpoint = Path(session) / 'egress_proxy.sock'
        mountpoint.parent.mkdir(parents=True, exist_ok=True)
        mountpoint.touch(exist_ok=True)
        argv += ['--bind', str(host_uds), '/session/egress_proxy.sock']
    if c.models_file:
        argv += ['--ro-bind', str(c.models_file), '/home/runner/.pi/agent/models.json']
    argv += ['--ro-bind', str(Path(__file__).with_name('policy.mjs')), '/runner/policy.mjs']
    forwarder_py = Path(__file__).with_name('forwarder.py')
    if forwarder_py.is_file():
        argv += ['--ro-bind', str(forwarder_py), '/runner/forwarder.py']
    # Knowledge and skills are explicit per-instance mounts. The Runner never
    # invents them from the host HOME directory.
    if c.knowledge_dir is not None:
        argv += ['--ro-bind', str(c.knowledge_dir), '/knowledge']
    if c.skills_dir is not None:
        argv += ['--ro-bind', str(c.skills_dir), '/skills']
    # The current-turn inbox view replaces the project's shared inbox inside
    # the sandbox. Earlier same-conversation assets stay read-only and are
    # never passed to the model as if they arrived with this turn.
    if c.inbox_dir is not None:
        argv += ['--ro-bind', str(c.inbox_dir), '/inbox']
    if c.attachments_dir is not None:
        argv += ['--ro-bind', str(c.attachments_dir), '/attachments']
    if c.outbox_dir is not None:
        argv += ['--bind', str(c.outbox_dir), '/outbox']
    dotgit = c.project / '.git'
    if dotgit.is_dir():
        if dotgit.is_symlink():
            raise RunnerError('Symlinked .git requires an ordinary checkout')
        git_bind = '--bind' if c.allow_git_commit else '--ro-bind'
        argv += [git_bind, str(dotgit), '/workspace/.git']
    elif dotgit.exists():
        # Linked worktrees need their external common directory exposed. Do not
        # silently mount it, since it may expose other projects or credentials.
        raise RunnerError('Linked worktree .git file is unsupported; use a standalone checkout')
    # The sandbox clears the environment, so every ro-bound runtime binary
    # is unreachable unless its own directory is on PATH: an ro-bound uv
    # under ~/.local/bin must not force agents to invent PATH prefixes.
    path_parts = [str(Path(c.pi_command[0]).parent)]
    for runtime_path in c.runtime_paths:
        entry = Path(runtime_path)
        if entry.is_dir():
            path_parts.append(str(entry))
            path_parts.append(str(entry / 'bin'))
        else:
            path_parts.append(str(entry.parent))
    path_parts += ['/usr/bin', '/bin']
    path = ':'.join(dict.fromkeys(path_parts))
    argv += ['--setenv', 'HOME', '/home/runner', '--setenv', 'PATH', path,
             '--setenv', 'LANG', 'C.UTF-8', '--setenv', 'TMPDIR', '/tmp',
             '--setenv', 'PI_CODING_AGENT_DIR', '/home/runner/.pi/agent',
             '--setenv', 'PI_OFFLINE', '1', '--setenv', 'PI_TELEMETRY', '0',
             '--setenv', 'GIT_CONFIG_NOSYSTEM', '1', '--setenv', 'GIT_CONFIG_GLOBAL', '/dev/null',
             '--setenv', 'GIT_TERMINAL_PROMPT', '0', '--chdir', '/workspace/' + c.workdir]
    for name in c.credential_env:
        if name not in os.environ:
            raise RunnerError(f'Missing credential environment variable: {name}')
        # The CLI passes all bwrap options through an anonymous fd, not argv.
        argv += ['--setenv', name, os.environ[name]]
    return argv


IGNORED_DIRECTORIES = frozenset({
    '.git',
    'node_modules',
    '.venv',
    'venv',
    '__pycache__',
    '.pytest_cache',
    '.ruff_cache',
    '.mypy_cache',
    'target',
    'dist',
    'build',
    '.cache',
})


def _sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _same_file_metadata(entry, st):
    return (
        isinstance(entry, dict)
        and entry.get('type') == 'file'
        and entry.get('st_size') == st.st_size
        and entry.get('st_mtime_ns') == st.st_mtime_ns
        and entry.get('st_ctime_ns') == st.st_ctime_ns
        and entry.get('st_mode') == st.st_mode & 0o777
    )


def _normalize_entry(entry):
    """Return a dict entry, including compatibility with legacy snapshots.

    Older ``before.json`` files stored files as ``"<sha256>:0o644"`` and
    symlinks as ``"link:<target>"``.  Those entries have no size/mtime/ctime,
    so a content hash is the only reliable way to compare them.  Treating
    them as dicts keeps ``changed_files`` total for both snapshot formats.
    """
    if isinstance(entry, dict):
        return entry
    if isinstance(entry, str):
        if entry.startswith('link:'):
            return {'type': 'symlink', 'target': entry[len('link:'):], 'legacy': True}
        digest, separator, mode = entry.rpartition(':')
        if separator and digest:
            try:
                return {
                    'type': 'file',
                    'sha256': digest,
                    'st_mode': int(mode, 8),
                    'legacy': True,
                }
            except ValueError:
                pass
    return {'type': 'legacy', 'value': entry}


def _same_entry(before, after):
    """Compare snapshot entries, accepting both current and legacy formats."""
    before = _normalize_entry(before)
    after = _normalize_entry(after)
    if before.get('type') != after.get('type'):
        return False
    if before.get('type') == 'symlink':
        return before.get('target') == after.get('target')
    if before.get('type') != 'file':
        return before == after

    # The current fingerprint includes ctime, which the kernel updates for
    # every inode write and which cannot be restored through os.utime.  This
    # catches same-size changes that preserve mtime (tar/rsync/copy tools).
    for field in ('st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_mode'):
        if field in before and field in after and before[field] != after[field]:
            return False

    before_hash = before.get('sha256')
    after_hash = after.get('sha256')
    if before_hash or after_hash:
        # A legacy baseline has no metadata; unless both sides carry a hash,
        # their content relationship is unknown and must be reported changed.
        return bool(before_hash and after_hash and before_hash == after_hash)
    return True


def snapshot(project, previous=None):
    """Return a metadata snapshot of ``project`` for change detection.

    Dependency, virtual-environment and build directories are not traversed.
    The baseline stores size, mtime, ctime and mode only, so a normal snapshot
    is a stat walk.  Content hashes are read only for legacy ``previous``
    entries (which lack ctime and may have only a hash), keeping old
    ``before.json`` sessions comparable without slowing new sessions down.
    """
    result = {}
    for root, dirs, files in os.walk(project, followlinks=False):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRECTORIES]
        for name in files + [d for d in dirs if (Path(root) / d).is_symlink()]:
            p = Path(root) / name
            relative = str(p.relative_to(project))
            try:
                st = p.lstat()
                if stat.S_ISLNK(st.st_mode):
                    value = {'type': 'symlink', 'target': os.readlink(p)}
                elif stat.S_ISREG(st.st_mode):
                    value = {
                        'type': 'file',
                        'st_size': st.st_size,
                        'st_mtime_ns': st.st_mtime_ns,
                        'st_ctime_ns': st.st_ctime_ns,
                        'st_mode': st.st_mode & 0o777,
                    }
                    old = _normalize_entry(previous.get(relative)) if previous is not None else None
                    if old and old.get('type') == 'file' and old.get('sha256'):
                        if 'st_ctime_ns' in old:
                            if _same_file_metadata(old, st):
                                value['sha256'] = old['sha256']
                        else:
                            # Legacy entries have no ctime, so content must be
                            # read to distinguish a real change from a stat-only
                            # difference.  New snapshots never take this path.
                            value['sha256'] = _sha256(p)
                else:
                    continue
                result[relative] = value
            except FileNotFoundError:
                continue
    return result


def changed_files(before, after):
    return sorted(
        p for p in before.keys() | after.keys()
        if p not in before or p not in after or not _same_entry(before[p], after[p])
    )
