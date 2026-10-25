#!/usr/bin/env python3
"""Exercise upgrade orchestration at the Docker process boundary, without a daemon."""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('billing_upgrade', ROOT / 'billing_upgrade.py')
upgrade = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upgrade)
IMAGE = 'ghcr.io/example/core@sha256:' + 'a' * 64
OLD = 'sha256:' + 'b' * 64
NEW = 'sha256:' + 'c' * 64


class Docker:
    def __init__(self):
        self.events = []
        self.version = 127
        self.candidate_schema = 129
        self.daemon_id = 'daemon-original'
        self.fail = None
        self.stop_failure = False
        self.items = {}
        for service in ('db', 'cache', 'core-api', 'core-edge', 'caddy'):
            self.items[service] = self.container(service)

    def container(self, service):
        return {'Id': service, 'Mounts': [{'Type': 'volume', 'Name': 'xbar-core_mysql-data',
                'Source': '/var/lib/docker/volumes/xbar-core_mysql-data/_data', 'Destination': '/var/lib/mysql'}]
                if service == 'db' else [], 'Name': '/xbar-core-' + service + '-1', 'Image': OLD,
                'Config': {'Image': 'old:tag', 'Entrypoint': ['/usr/local/bin/mirror-api'] if service == 'core-api' else [],
                           'Cmd': [], 'Labels': {'com.docker.compose.project': 'xbar-core',
                           'com.docker.compose.service': service}},
                'State': {'Running': True, 'Status': 'running', 'ExitCode': 0, 'OOMKilled': False,
                          'Health': {'Status': 'healthy'}}}

    def __call__(self, argv, **kwargs):
        argv = [str(x) for x in argv]
        if argv[:3] == ['docker', 'compose', '--project-name']:
            cmd = argv[argv.index('--file') + 2:]
            # Explicit --env-file precedes --file.
            if cmd == ['config', '--format', 'json']:
                services = {s: {'image': IMAGE if s.startswith('core-') else s + ':latest',
                               'healthcheck': {'test': ['CMD', 'true']},
                               'environment': {'MYSQL_DATABASE': 'xbar', 'PASSWORD': 'DO-NOT-PRINT'}}
                            for s in self.items if s in ('db', 'cache', 'core-api', 'core-edge', 'caddy')}
                return self.result(json.dumps({'services': services}))
            if cmd[0] == 'up':
                service = cmd[-1]
                self.events.append('start:' + service)
                if service == 'core-api':
                    self.version = 128 if self.fail == 'schema' else self.candidate_schema
                self.items[service]['State']['Running'] = True
                self.items[service]['State']['Status'] = 'running'
                self.items[service]['Image'] = NEW
                self.items[service]['Config']['Image'] = IMAGE
                if self.fail == 'candidate' and service == 'core-api':
                    raise subprocess.CalledProcessError(1, argv, stderr='DO-NOT-PRINT')
                return self.result()
        elif argv[:2] == ['docker', 'info']:
            return self.result(self.daemon_id)
        elif argv[:3] == ['docker', 'ps', '-aq']:
            return self.result('\n'.join(self.items))
        elif argv[:2] == ['docker', 'inspect']:
            return self.result(json.dumps([self.items[x] for x in argv[2:]]))
        elif argv[:2] == ['docker', 'pull']:
            self.events.append('pull')
            if self.fail == 'pull':
                raise subprocess.CalledProcessError(1, argv)
            return self.result()
        elif argv[:3] == ['docker', 'image', 'inspect']:
            return self.result(json.dumps([{'Id': NEW, 'RepoDigests': [IMAGE]}]))
        elif argv[:2] == ['docker', 'stop']:
            service = argv[-1]
            self.events.append('stop:' + service)
            if self.stop_failure and service == 'core-api' and 'start:core-api' in self.events:
                raise subprocess.CalledProcessError(1, argv)
            self.items[service]['State'].update(Running=False, Status='exited', ExitCode=0)
            if self.fail == 'drain' and service == 'core-edge':
                self.items[service]['State']['ExitCode'] = 137
            return self.result()
        elif argv[:2] == ['docker', 'exec']:
            script = argv[-1]
            if 'mysqldump' in script:
                self.events.append('backup')
                if self.fail == 'backup':
                    raise subprocess.CalledProcessError(1, argv)
                kwargs['stdout'].write(b'-- MySQL dump\nCREATE TABLE `goose_db_version` (id int);\n' +
                                       (b'' if self.fail == 'truncated_backup' else b'-- Dump completed on 2026-10-16\n'))
                return self.result()
            if 'SELECT MAX(version_id)' in script:
                return self.result(str(self.version))
        raise AssertionError('Unexpected command: ' + repr(argv))

    @staticmethod
    def result(stdout=''):
        return subprocess.CompletedProcess([], 0, stdout, '')


class UpgradeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.compose = self.root / 'compose.yml'
        self.compose.write_text('services: {}\n')
        self.env = self.root / '.env'
        self.env.write_text('PASSWORD=DO-NOT-PRINT\n')
        self.args = ['--compose-file', str(self.compose), '--env-file', str(self.env),
                     '--state-dir', str(self.root / 'state'), '--image', IMAGE]
        self.docker = Docker()

    def run_upgrade(self, *args):
        out = io.StringIO()
        with patch.object(upgrade.subprocess, 'run', self.docker), patch('sys.stdout', out):
            upgrade.main(self.args + list(args))
        self.assertNotIn('DO-NOT-PRINT', out.getvalue())
        return out.getvalue()

    def test_default_plan_is_read_only(self):
        self.run_upgrade()
        self.assertEqual(self.docker.events, [])
        self.assertFalse((self.root / 'state').exists())

    def test_digest_is_required_before_any_mutation(self):
        self.args[-1] = 'ghcr.io/example/core:latest'
        with self.assertRaises((SystemExit, RuntimeError)):
            self.run_upgrade('--apply')
        self.assertEqual(self.docker.events, [])

    def test_success_drains_then_backs_up_before_migration(self):
        self.run_upgrade('--apply')
        self.assertEqual(self.docker.events, ['pull', 'stop:core-edge', 'stop:core-api',
                                             'backup', 'start:core-api', 'start:core-edge'])
        state = json.loads((self.root / 'state/state.json').read_text())
        self.assertEqual(state['phase'], 'complete')
        self.assertEqual(state['schema'], 129)
        self.assertTrue(Path(state['backup']['path']).is_file())

    def test_additive_schema130_candidate_and_completed_resume(self):
        self.docker.candidate_schema = 130
        self.run_upgrade('--apply')
        self.assertEqual(json.loads((self.root / 'state/state.json').read_text())['schema'], 130)
        self.docker.events.clear()
        self.run_upgrade('--apply', '--resume')
        self.assertEqual(self.docker.events, [])

    def test_additive_schema131_candidate_and_completed_resume(self):
        self.docker.candidate_schema = 131
        self.run_upgrade('--apply')
        self.assertEqual(json.loads((self.root / 'state/state.json').read_text())['schema'], 131)
        self.docker.events.clear()
        self.run_upgrade('--apply', '--resume')
        self.assertEqual(self.docker.events, [])

    def test_fresh_post127_schema_with_old_api_is_rejected_without_mutation(self):
        for version in (128, 129, 130, 131):
            with self.subTest(schema=version):
                self.docker.version = version
                with self.assertRaisesRegex(RuntimeError, 'schema 127'):
                    self.run_upgrade('--apply')
                self.assertEqual(self.docker.events, [])
                self.assertTrue(self.docker.items['core-api']['State']['Running'])
                self.assertFalse((self.root / 'state/state.json').exists())

    def test_failed_pull_never_stops_live_services(self):
        self.docker.fail = 'pull'
        with self.assertRaises(RuntimeError):
            self.run_upgrade('--apply')
        self.assertEqual(self.docker.events, ['pull'])
        self.assertTrue(self.docker.items['core-api']['State']['Running'])

    def test_failed_backup_does_not_start_any_api(self):
        self.docker.fail = 'backup'
        with self.assertRaises(RuntimeError):
            self.run_upgrade('--apply')
        self.assertEqual(self.docker.version, 127)
        self.assertNotIn('start:core-api', self.docker.events)
        self.assertFalse(self.docker.items['core-api']['State']['Running'])

    def test_failed_candidate_never_rolls_back_and_leaves_services_stopped(self):
        self.docker.fail = 'candidate'
        with self.assertRaises(RuntimeError):
            self.run_upgrade('--apply')
        self.assertEqual(self.docker.version, 129)
        self.assertEqual(self.docker.events.count('start:core-api'), 1)
        self.assertFalse(self.docker.items['core-api']['State']['Running'])
        self.assertFalse(self.docker.items['core-edge']['State']['Running'])
        self.assertEqual(self.docker.items['core-api']['Image'], NEW)

    def test_interrupted_upgrade_requires_explicit_resume_and_keeps_original_backup(self):
        self.docker.fail = 'candidate'
        with self.assertRaises(RuntimeError):
            self.run_upgrade('--apply')
        events = list(self.docker.events)
        with self.assertRaisesRegex(RuntimeError, 'resume'):
            self.run_upgrade('--apply')
        self.assertEqual(events, self.docker.events)
        self.docker.fail = None
        self.run_upgrade('--apply', '--resume')
        self.assertEqual(self.docker.events.count('backup'), 1)
        self.assertEqual(self.docker.version, 129)

    def test_duplicate_or_foreign_api_is_rejected_before_pull(self):
        for foreign in (False, True):
            with self.subTest(foreign=foreign):
                extra = self.docker.container('core-api')
                if foreign:
                    extra['Config']['Labels']['com.docker.compose.project'] = 'rolling'
                self.docker.items['extra'] = extra
                with self.assertRaises(RuntimeError):
                    self.run_upgrade('--apply')
                self.assertEqual(self.docker.events, [])

    def test_forced_edge_shutdown_prevents_migration(self):
        self.docker.fail = 'drain'
        with self.assertRaises(RuntimeError):
            self.run_upgrade('--apply')
        self.assertNotIn('backup', self.docker.events)
        self.assertNotIn('start:core-api', self.docker.events)
        self.assertEqual(self.docker.version, 127)

    def test_incomplete_successful_dump_does_not_start_candidate(self):
        self.docker.fail = 'truncated_backup'
        with self.assertRaisesRegex(RuntimeError, 'incomplete'):
            self.run_upgrade('--apply')
        self.assertNotIn('start:core-api', self.docker.events)

    def test_wrong_schema_stops_candidate_before_edge_starts(self):
        self.docker.fail = 'schema'
        with self.assertRaisesRegex(RuntimeError, 'schema'):
            self.run_upgrade('--apply')
        self.assertNotIn('start:core-edge', self.docker.events)
        self.assertFalse(self.docker.items['core-api']['State']['Running'])

    def test_environment_remains_pinned_even_when_migrating_candidate_fails(self):
        self.docker.fail = 'candidate'
        with self.assertRaises(RuntimeError):
            self.run_upgrade('--apply')
        self.assertIn('XBAR_CORE_IMAGE=' + IMAGE, self.env.read_text())
        self.assertIn('PASSWORD=DO-NOT-PRINT', self.env.read_text())

    def test_resume_completed_upgrade_verifies_without_new_downtime(self):
        self.run_upgrade('--apply')
        events = list(self.docker.events)
        self.run_upgrade('--apply', '--resume')
        self.assertEqual(events, self.docker.events)

    def test_schema_changed_before_migration_blocks_resume(self):
        self.docker.fail = 'backup'
        with self.assertRaises(RuntimeError):
            self.run_upgrade('--apply')
        self.docker.fail = None
        self.docker.version = 129
        events = list(self.docker.events)
        with self.assertRaisesRegex(RuntimeError, 'schema'):
            self.run_upgrade('--apply', '--resume')
        self.assertEqual(events, self.docker.events)

    def test_reverted_image_pin_after_migration_blocks_resume(self):
        self.docker.fail = 'candidate'
        with self.assertRaises(RuntimeError):
            self.run_upgrade('--apply')
        self.env.write_text('PASSWORD=DO-NOT-PRINT\n')
        events = list(self.docker.events)
        with self.assertRaisesRegex(RuntimeError, 'Environment'):
            self.run_upgrade('--apply', '--resume')
        self.assertEqual(events, self.docker.events)

    def prepare_failed_migration(self):
        self.docker.fail = 'candidate'
        with self.assertRaises(RuntimeError):
            self.run_upgrade('--apply')
        self.docker.fail = None
        return list(self.docker.events)

    def test_changed_docker_daemon_blocks_resume_before_mutation(self):
        events = self.prepare_failed_migration()
        self.docker.daemon_id = 'another-daemon'
        with self.assertRaisesRegex(RuntimeError, 'identity'):
            self.run_upgrade('--apply', '--resume')
        self.assertEqual(events, self.docker.events)

    def test_replaced_database_container_blocks_resume_before_mutation(self):
        events = self.prepare_failed_migration()
        self.docker.items['db']['Id'] = 'replacement-database'
        with self.assertRaisesRegex(RuntimeError, 'identity'):
            self.run_upgrade('--apply', '--resume')
        self.assertEqual(events, self.docker.events)

    def test_replaced_database_volume_blocks_resume_before_mutation(self):
        events = self.prepare_failed_migration()
        self.docker.items['db']['Mounts'][0]['Source'] = '/var/lib/docker/volumes/replacement/_data'
        with self.assertRaisesRegex(RuntimeError, 'identity'):
            self.run_upgrade('--apply', '--resume')
        self.assertEqual(events, self.docker.events)

    def test_cleanup_warning_survives_checkpoint_failure_and_preserves_candidate_error(self):
        self.docker.fail = 'candidate'
        self.docker.stop_failure = True
        original_write = upgrade.atomic_write

        def fail_failed_checkpoint(path, data, *args):
            if Path(path).name == 'state.json' and json.loads(data)['phase'] == 'failed':
                raise OSError('DO-NOT-PRINT: disk full')
            return original_write(path, data, *args)

        out = io.StringIO()
        with patch.object(upgrade.subprocess, 'run', self.docker), patch('sys.stdout', out), \
                patch.object(upgrade, 'atomic_write', fail_failed_checkpoint):
            with self.assertRaisesRegex(RuntimeError, 'Docker operation failed \(compose\)'):
                upgrade.main(self.args + ['--apply'])
        self.assertIn('STOP REQUIRED', out.getvalue())
        self.assertIn('Checkpoint', out.getvalue())
        self.assertNotIn('DO-NOT-PRINT', out.getvalue())
        self.assertTrue(self.docker.items['core-api']['State']['Running'])

    def test_tampered_backup_blocks_resume(self):
        self.docker.fail = 'candidate'
        with self.assertRaises(RuntimeError):
            self.run_upgrade('--apply')
        state = json.loads((self.root / 'state/state.json').read_text())
        Path(state['backup']['path']).write_text('broken backup')
        self.docker.fail = None
        events = list(self.docker.events)
        with self.assertRaisesRegex(RuntimeError, 'backup'):
            self.run_upgrade('--apply', '--resume')
        self.assertEqual(events, self.docker.events)


if __name__ == '__main__':
    unittest.main()
