#!/usr/bin/env python3
"""Coordinated, downtime-required registry Compose upgrade to billing schema 129.

Run on the Docker host. Fresh deployments require schema 127. Schema 128/129/130/131
is accepted only when resuming this tool's durable migration-start checkpoint;
an old API running after migration 128 can invalidate its legacy backfill.
Default is a read-only plan. --apply drains Edge and
API, takes a completed SQL dump with a durable SHA256, pins XBAR_CORE_IMAGE in
--env-file, then starts API (which migrates) before Edge. --resume explicitly
retries an interrupted deployment with the SAME digest. Never rolls back an
image or database. Backup checks establish dump completeness and integrity,
not a tested restore. Keep an independently tested restore procedure.
Resume also requires the original Docker daemon, database container and data
mount identity. Replaced databases require manual recovery, not --resume.

Supports one xbar-core project with core-api/core-edge/db/cache and optional
caddy, on one local Docker daemon. External hosts, unmanaged host processes,
and concurrent operators are outside its inventory; fence those separately.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

SERVICES = ('core-api', 'core-edge', 'db', 'cache')
SCHEMA = 129
SUPPORTED_FINAL_SCHEMAS = (129, 130, 131)


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def atomic_write(path, text, mode=0o600):
    path = Path(path)
    temporary = path.with_name(path.name + '.new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, 'w') as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class Upgrade:
    def __init__(self, args):
        self.args = args
        self.directory = Path(args.state_dir).resolve()
        self.state_file = self.directory / 'state.json'
        self.env_file = Path(args.env_file).resolve()
        self.compose_file = Path(args.compose_file).resolve()
        self.environment = os.environ.copy()
        self.environment.update(XBAR_CORE_IMAGE=args.image, XBAR_CORE_ENV_FILE=str(self.env_file))
        self.base = ['docker', 'compose', '--project-name', args.project,
                     '--env-file', str(self.env_file), '--file', str(self.compose_file)]
        self.state = None

    def run(self, command, *, stdout=None, timeout=120):
        try:
            result = subprocess.run(command, env=self.environment, check=True,
                                    stdout=stdout if stdout is not None else subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=stdout is None, timeout=timeout)
            return result.stdout.strip() if stdout is None else ''
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            # Docker/Compose diagnostics may contain expanded environment secrets.
            raise RuntimeError('Docker operation failed (' + command[1] +
                               '); inspect host diagnostics privately') from None

    def save(self, phase=None):
        if phase:
            self.state['phase'] = phase
        atomic_write(self.state_file, json.dumps(self.state, indent=2) + '\n')

    def inventory(self):
        ids = self.run(['docker', 'ps', '-aq']).split()
        return json.loads(self.run(['docker', 'inspect', *ids])) if ids else []

    def topology(self):
        containers = self.inventory()
        found = {service: [] for service in SERVICES}
        for item in containers:
            config = item['Config']
            labels = config.get('Labels') or {}
            service = labels.get('com.docker.compose.service', '')
            project = labels.get('com.docker.compose.project')
            if project == self.args.project:
                if service not in (*SERVICES, 'caddy'):
                    raise RuntimeError('Unknown service in target Compose project')
                if service in found:
                    found[service].append(item)
            else:
                command = ' '.join(config.get('Entrypoint') or []) + ' ' + ' '.join(config.get('Cmd') or [])
                suspect = (service in ('core-api', 'core-edge', 'api', 'chatgpt') or
                           re.search(r'mirror-(api|edge)|xbar-(api|chatgpt)', command + ' ' + item['Name']) or
                           config.get('Image') == self.args.image)
                if item['State']['Running'] and suspect:
                    raise RuntimeError('Foreign API/Edge or rolling overlap detected; stop it explicitly')
        for service, values in found.items():
            if len(values) != 1:
                raise RuntimeError('Expected exactly one container for ' + service)
        return {key: value[0] for key, value in found.items()}

    def validate_config(self):
        config = json.loads(self.run([*self.base, 'config', '--format', 'json']))
        services = config['services']
        if not set(SERVICES) <= set(services) or set(services) - set((*SERVICES, 'caddy')):
            raise RuntimeError('Unsupported Compose service topology')
        for service in ('core-api', 'core-edge'):
            value = services[service]
            if value.get('image') != self.args.image or value.get('build'):
                raise RuntimeError('Compose must use the pinned XBAR_CORE_IMAGE without a build section')
            health = value.get('healthcheck', {})
            if health.get('disable') or not health.get('test') or health['test'][0] == 'NONE':
                raise RuntimeError('API and Edge require enabled healthchecks')
            if value.get('deploy', {}).get('replicas', 1) != 1 or value.get('scale', 1) != 1:
                raise RuntimeError('API and Edge must each have one replica')
        if services['db'].get('environment', {}).get('MYSQL_DATABASE') != 'xbar':
            raise RuntimeError('Expected MySQL database xbar')

    def schema(self, db):
        value = self.run(['docker', 'exec', db['Id'], 'sh', '-c',
                         'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; exec mysql -uroot -N -B xbar '
                         '-e "SELECT MAX(version_id) FROM goose_db_version WHERE is_applied=1"'])
        if not value.isdigit():
            raise RuntimeError('Cannot identify database migration version')
        return int(value)

    def database_identity(self, db):
        daemon = self.run(['docker', 'info', '--format', '{{.ID}}'])
        mounts = [mount for mount in db.get('Mounts', []) if mount.get('Destination') == '/var/lib/mysql']
        if not daemon or len(mounts) != 1 or mounts[0].get('Type') not in ('volume', 'bind'):
            raise RuntimeError('Cannot establish Docker/database persistent storage identity')
        mount = {key: mounts[0].get(key) for key in ('Type', 'Name', 'Source', 'Destination')}
        if not mount['Source'] or not db.get('Id'):
            raise RuntimeError('Incomplete database identity')
        return {'daemon_id': daemon, 'container_id': db['Id'], 'data_mount': mount}

    def healthy(self, item):
        return item['State']['Running'] and item['State'].get('Health', {}).get('Status') == 'healthy'

    def stop(self, service):
        item = self.topology()[service]
        if item['State']['Running']:
            self.run(['docker', 'stop', '--time', str(self.args.drain_timeout), item['Id']],
                     timeout=self.args.drain_timeout + 60)
            item = self.topology()[service]
            if item['State']['Running']:
                raise RuntimeError(service + ' did not stop')
            if item['State'].get('ExitCode') == 137 or item['State'].get('OOMKilled'):
                raise RuntimeError(service + ' was killed instead of draining; inspect before resuming')

    def backup(self, db):
        path = self.directory / ('before-schema129-' + str(time.time_ns()) + '.sql')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            self.run(['docker', 'exec', db['Id'], 'sh', '-c',
                      'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; exec mysqldump -uroot '
                      '--single-transaction --routines --events --triggers --hex-blob '
                      '--set-gtid-purged=OFF --databases xbar'], stdout=stream, timeout=self.args.backup_timeout)
            stream.flush()
            os.fsync(stream.fileno())
        with path.open('rb') as stream:
            header = stream.read(4096)
            stream.seek(max(0, path.stat().st_size - 4096))
            tail = stream.read()
        if not header.startswith(b'-- MySQL dump') or b'-- Dump completed on ' not in tail:
            raise RuntimeError('Database backup is incomplete')
        self.state['backup'] = {'path': str(path), 'sha256': digest(path), 'bytes': path.stat().st_size}
        self.save('backed_up')

    def verify_backup(self):
        backup = self.state.get('backup', {})
        path = Path(backup.get('path', ''))
        if not path.is_file() or path.stat().st_size != backup.get('bytes') or digest(path) != backup.get('sha256'):
            raise RuntimeError('Original backup missing or modified; cannot resume')

    def pin_environment(self):
        current = self.env_file.read_text()
        pattern = r'(?m)^\s*(?:export\s+)?XBAR_CORE_IMAGE\s*=.*$'
        replacement = 'XBAR_CORE_IMAGE=' + self.args.image
        pinned = re.sub(pattern, replacement, current) if re.search(pattern, current) else current.rstrip('\n') + '\n' + replacement + '\n'
        self.state['pinned_env_sha256'] = hashlib.sha256(pinned.encode()).hexdigest()
        self.save()
        atomic_write(self.env_file, pinned, self.env_file.stat().st_mode & 0o777)

    def start(self, service):
        self.run([*self.base, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never',
                  '--wait', '--wait-timeout', str(self.args.health_timeout), service],
                 timeout=self.args.health_timeout + 60)
        item = self.topology()[service]
        if not self.healthy(item) or item['Image'] != self.state['image_id'] or item['Config']['Image'] != self.args.image:
            raise RuntimeError(service + ' health or immutable image verification failed')

    def execute(self):
        if self.state_file.exists():
            self.state = json.loads(self.state_file.read_text())
            if self.args.apply and not self.args.resume:
                raise RuntimeError('Deployment checkpoint exists; inspect it and use --resume explicitly')
            if self.state['image'] != self.args.image or self.state['project'] != self.args.project:
                raise RuntimeError('Checkpoint target differs; cannot resume with another image/project')
            if self.state['compose_sha256'] != digest(self.compose_file) or self.state['compose_file'] != str(self.compose_file):
                raise RuntimeError('Compose configuration changed since checkpoint')
            allowed_env_hashes = (self.state.get('pinned_env_sha256'),)
            if not self.state.get('migration_started'):
                allowed_env_hashes += (self.state['env_sha256'],)
            if self.state['env_file'] != str(self.env_file) or digest(self.env_file) not in allowed_env_hashes:
                raise RuntimeError('Environment changed since checkpoint')
            if self.state.get('migration_started'):
                self.verify_backup()
        elif self.args.resume:
            raise RuntimeError('No checkpoint to resume')
        self.validate_config()
        items = self.topology()
        identity = self.database_identity(items['db'])
        if self.state and self.state.get('database_identity') != identity:
            raise RuntimeError('Docker/database identity changed or missing from checkpoint; inspect manually')
        if not all(self.healthy(items[service]) for service in ('db', 'cache')):
            raise RuntimeError('Database and cache must be healthy')
        version = self.schema(items['db'])
        if version not in (127, 128, *SUPPORTED_FINAL_SCHEMAS):
            raise RuntimeError('Expected existing schema 127, 128, 129, 130 or 131')
        if version != 127 and not (self.state and self.state.get('migration_started')):
            raise RuntimeError('Fresh deployment requires schema 127; schema 128/129/130/131 needs a trusted migration checkpoint')
        if self.state and not self.state.get('migration_started') and version != self.state['original_schema']:
            raise RuntimeError('Database schema changed before migration; inspect manually')
        if self.state and self.state['phase'] == 'complete':
            if version != self.state.get('schema', SCHEMA) or not all(self.healthy(items[s]) for s in SERVICES):
                raise RuntimeError('Completed deployment schema/health has changed; inspect manually')
            for service in ('core-api', 'core-edge'):
                if items[service]['Image'] != self.state['image_id'] or items[service]['Config']['Image'] != self.args.image:
                    raise RuntimeError('Completed deployment image has changed; inspect manually')
            print(f'Completed deployment verified: schema {version}, pinned API/Edge healthy.')
            return
        if not self.args.apply:
            print('PLAN: pull pinned image; drain Edge then API; verify SQL backup; pin environment; '
                  'start API and require schema 129/130/131 + health; start Edge and verify images/health. '
                  'Downtime required; no automatic rollback. Current schema: ' + str(version))
            return
        if not self.state:
            self.state = {'image': self.args.image, 'project': self.args.project,
                          'compose_file': str(self.compose_file), 'compose_sha256': digest(self.compose_file),
                          'env_file': str(self.env_file), 'env_sha256': digest(self.env_file),
                          'original_schema': version, 'migration_started': False,
                          'database_identity': identity}
            # Bind the backup and migration intent to this database before any
            # mutating Docker command, including image acquisition.
            self.save('preflight')
        self.run(['docker', 'pull', self.args.image], timeout=self.args.pull_timeout)
        image = json.loads(self.run(['docker', 'image', 'inspect', self.args.image]))[0]
        if self.args.image not in image.get('RepoDigests', []):
            raise RuntimeError('Pulled image digest was not verified')
        self.state['image_id'] = image['Id']
        self.save('draining')
        try:
            self.stop('core-edge')
            self.stop('core-api')
            items = self.topology()
            if any(items[s]['State']['Running'] for s in ('core-api', 'core-edge')):
                raise RuntimeError('API/Edge still running before backup/migration')
            self.save('drained')
            if not self.state.get('migration_started'):
                self.backup(items['db'])
            self.verify_backup()
            self.pin_environment()
            # Durable intent BEFORE startup: startup can commit destructive SQL
            # even if Compose fails, the process dies, or health never succeeds.
            self.state['migration_started'] = True
            self.save('migrating')
            self.start('core-api')
            if self.schema(self.topology()['db']) not in SUPPORTED_FINAL_SCHEMAS:
                raise RuntimeError('Candidate did not reach supported schema 129/130/131')
            self.save('api_healthy')
            self.start('core-edge')
            items = self.topology()
            final_schema = self.schema(items['db'])
            if final_schema not in SUPPORTED_FINAL_SCHEMAS or not all(self.healthy(items[s]) for s in SERVICES):
                raise RuntimeError('Final schema/health verification failed')
            for service in ('core-api', 'core-edge'):
                if items[service]['Image'] != image['Id']:
                    raise RuntimeError('Final image verification failed')
            self.state['schema'] = final_schema
            self.save('complete')
            print(f'Upgrade complete: schema {final_schema}, pinned API/Edge healthy. Backup: ' + self.state['backup']['path'])
        except BaseException:
            failures = []
            # A failed candidate may be restarting. Stop it, never the old image.
            for service in ('core-edge', 'core-api'):
                try:
                    self.stop(service)
                except BaseException:
                    failures.append(service)
            self.state['cleanup_failed'] = failures
            if failures:
                print('STOP REQUIRED: could not confirm stopped services: ' + ', '.join(failures))
            try:
                self.save('failed')
            except Exception:
                # A full disk must not suppress a failed-stop warning or mask
                # the original deployment error with a checkpoint write error.
                print('Checkpoint update failed; inspect services and the previous checkpoint before resuming.')
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--compose-file', required=True)
    parser.add_argument('--env-file', required=True)
    parser.add_argument('--image', required=True, help='registry repository@sha256:<64 hex>, already built and published')
    parser.add_argument('--project', default='xbar-core')
    parser.add_argument('--state-dir', default='/var/lib/xbar-core/billing-upgrade-129')
    parser.add_argument('--apply', action='store_true', help='perform the coordinated deployment')
    parser.add_argument('--resume', action='store_true', help='explicitly retry the SAME digest after inspecting checkpoint')
    parser.add_argument('--drain-timeout', type=int, default=1860)
    parser.add_argument('--health-timeout', type=int, default=180)
    parser.add_argument('--backup-timeout', type=int, default=1800)
    parser.add_argument('--pull-timeout', type=int, default=900)
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:[0-9a-f]{64}', args.image):
        raise RuntimeError('Target image must be pinned to a sha256 digest')
    if min(args.drain_timeout, args.health_timeout, args.backup_timeout, args.pull_timeout) < 1:
        raise RuntimeError('Timeouts must be positive')
    if args.resume and not args.apply:
        raise RuntimeError('--resume requires --apply')
    deployment = Upgrade(args)
    if not args.apply:
        deployment.execute()
        return
    deployment.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (deployment.directory / 'upgrade.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another coordinated upgrade holds the lock') from None
        deployment.execute()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('FAILED: ' + str(error), file=sys.stderr)
        sys.exit(1)
