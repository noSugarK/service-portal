"""Run: python3 -m unittest -v"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import portal


class Tests(unittest.TestCase):
    def setUp(self):
        self.cfg = portal.load_config(Path(__file__).with_name('config.json'))

    def test_ss_ipv4_ipv6_and_loopback(self):
        rows = '''LISTEN 0 128 0.0.0.0:3000 0.0.0.0:*
LISTEN 0 128 [::]:443 [::]:*
LISTEN 0 128 127.0.0.1:8080 0.0.0.0:*
LISTEN 0 128 [::1]:9000 [::]:*
LISTEN 0 128 192.168.1.10:8000 0.0.0.0:*
LISTEN 0 128 *:5000 *:*
garbage'''
        self.assertEqual(set(portal.parse_listeners(rows)), {
            ('127.0.0.1', 3000, False), ('::1', 443, False),
            ('127.0.0.1', 8080, True), ('::1', 9000, True),
            ('192.168.1.10', 8000, False), ('127.0.0.1', 5000, False)})

    def test_probe_title_redirect_auth_and_alias(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                requests.append(self.path)
                self.send_response(302 if self.path == '/redirect' else 401)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Location', 'http://must-not-follow.invalid/')
                self.end_headers()
                self.wfile.write('<title>测试 &amp; 管理台</title>'.encode())

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_port
        try:
            item = portal.probe(('127.0.0.1', port, False), self.cfg)
            self.assertEqual(item['name'], '测试 & 管理台')
            self.assertEqual(item['status'], 401)
            self.cfg['services'][str(port)] = {'name': '自定义名称', 'path': '/redirect'}
            item = portal.probe(('127.0.0.1', port, False), self.cfg)
            self.assertEqual((item['name'], item['status']), ('自定义名称', 302))
            self.assertEqual(requests, ['/', '/redirect'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_filter_self_excluded_and_loopback_and_deduplicate(self):
        rows = '''LISTEN 0 128 0.0.0.0:80 *:*
LISTEN 0 128 0.0.0.0:22 *:*
LISTEN 0 128 127.0.0.1:8080 *:*
LISTEN 0 128 0.0.0.0:3000 *:*
LISTEN 0 128 [::]:3000 *:*'''

        def fake_probe(endpoint, cfg):
            return dict(port=endpoint[1], scheme='http', loopback=endpoint[2])

        with patch('portal.subprocess.run') as run, patch('portal.probe', side_effect=fake_probe) as probe:
            run.return_value.stdout = rows
            self.assertEqual(len(portal.scan(self.cfg)), 1)
            self.assertEqual(probe.call_count, 2)

    def test_bad_paths_and_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            for bad in ['//evil.example/path', 'https://evil.example', '/\\evil', '/\n']:
                self.cfg['services'] = {'3000': {'path': bad}}
                path.write_text(json.dumps(self.cfg), encoding='utf-8')
                with self.assertRaises(ValueError):
                    portal.load_config(path)
            self.cfg['services'] = {}
            self.cfg['port'] = 0
            path.write_text(json.dumps(self.cfg), encoding='utf-8')
            with self.assertRaises(ValueError):
                portal.load_config(path)

    def test_protocol_selection_and_non_http(self):
        with patch('portal.http.client.HTTPConnection') as http, patch('portal.http.client.HTTPSConnection') as https:
            response = https.return_value.getresponse.return_value
            response.status = 200
            response.getheader.return_value = 'text/plain'
            item = portal.probe(('127.0.0.1', 443, False), self.cfg)
            self.assertEqual(item['scheme'], 'https')
            http.assert_not_called()
            https.return_value.close.assert_called_once()
            https.return_value.getresponse.side_effect = OSError('not TLS')
            http.return_value.getresponse.return_value.status = 403
            http.return_value.getresponse.return_value.getheader.return_value = 'text/plain'
            self.assertEqual(portal.probe(('127.0.0.1', 80, False), self.cfg)['scheme'], 'http')
            http.return_value.getresponse.side_effect = portal.http.client.BadStatusLine('SSH-2.0')
            self.assertIsNone(portal.probe(('127.0.0.1', 22, False), self.cfg))

    @contextmanager
    def running_portal(self, docker=False):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.cfg.update(bind='127.0.0.1', port=port, docker_enabled=docker)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps(self.cfg), encoding='utf-8')
            # Isolate platform-specific ss discovery; exercise the real HTTP process.
            command = 'import portal; portal.scan=lambda cfg: []; portal.docker_operation=lambda method,path: dict(method=method,path=path); portal.main()'
            process = subprocess.Popen([sys.executable, '-c', command, '--config', str(path)],
                                       cwd=Path(__file__).parent, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       env=dict(os.environ, SERVICE_PORTAL_DOCKER_TOKEN='a' * 40))
            try:
                base = f'http://127.0.0.1:{port}'
                for attempt in range(50):
                    try:
                        with urlopen(base + '/healthz', timeout=1) as response:
                            self.assertEqual(response.read(), b'ok\n')
                        break
                    except URLError:
                        time.sleep(0.1)
                else:
                    self.fail('HTTP server did not start')
                yield base
            finally:
                process.terminate()
                process.wait(timeout=5)

    def test_http_entry_and_api(self):
        with self.running_portal() as base:
            for path in ('/', '/docker'):
                with urlopen(base + path, timeout=2) as response:
                    self.assertIn('本机服务导航', response.read().decode())
                    self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
            with urlopen(base + '/api/services', timeout=2) as response:
                data = json.load(response)
                self.assertEqual(data['services'], [])
                self.assertEqual(data['scan_interval'], 60)
            with self.assertRaises(HTTPError) as error:
                urlopen(base + '/api/docker/containers', timeout=2)
            self.assertEqual(error.exception.code, 403)

    def test_docker_auth_and_origin(self):
        with self.running_portal(docker=True) as base:
            route = base + '/api/docker/containers'
            for token in ('', 'wrong', '令牌'):
                # HTTP headers use latin-1; use a non-ASCII byte sequence for robustness.
                token = token.encode().decode('latin-1')
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(route, headers={'Authorization': 'Bearer ' + token}), timeout=2)
                self.assertEqual(error.exception.code, 401)
            headers = {'Authorization': 'Bearer ' + 'a' * 40}
            with urlopen(Request(route, headers=headers), timeout=2) as response:
                self.assertEqual(json.load(response)['method'], 'GET')
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
            action = route + '/' + 'f' * 64 + '/restart'
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(action, method='POST', headers=dict(headers, Origin='https://evil.example')), timeout=2)
            self.assertEqual(error.exception.code, 403)
            with urlopen(Request(action, method='POST', headers=dict(headers, Origin=base)), timeout=2) as response:
                self.assertEqual(json.load(response)['method'], 'POST')

    def test_docker_requires_strong_configured_token(self):
        self.cfg['docker_enabled'] = True
        for token in ('', 'short', 'a' * 32 + '\n', '令' * 32):
            with patch('portal.load_config', return_value=self.cfg), patch('sys.argv', ['portal.py']), \
                 patch.dict(os.environ, SERVICE_PORTAL_DOCKER_TOKEN=token):
                with self.assertRaisesRegex(ValueError, 'SERVICE_PORTAL_DOCKER_TOKEN'):
                    portal.main()

    def test_docker_operations_and_logs(self):
        container = 'a' * 64
        with patch('portal.docker_request') as request:
            request.return_value = (json.dumps([dict(Id=container, Names=['/web'], Image='nginx', State='running',
                                                    Status='Up', Ports=[dict(PublicPort=8080, PrivatePort=80)])]).encode(), False)
            item = portal.docker_operation('GET', '/api/docker/containers')['containers'][0]
            self.assertEqual((item['name'], item['ports']), ('web', '8080:80/tcp'))
            for action in ('start', 'stop', 'restart'):
                portal.docker_operation('POST', f'/api/docker/containers/{container}/{action}')
                request.assert_called_with('POST', f'/containers/{container}/{action}' + ('?t=10' if action != 'start' else ''))
            log = '测试日志\n'.encode()
            frame = b'\x01\0\0\0' + len(log).to_bytes(4, 'big') + log
            for raw in (log, frame):
                request.return_value = (raw, False)
                self.assertEqual(portal.docker_operation('GET', f'/api/docker/containers/{container}/logs')['logs'], log.decode())
            for method, path in [('GET', f'/api/docker/containers/{container}/stop'), ('POST', f'/api/docker/containers/{container}/logs'),
                                 ('POST', '/api/docker/containers/--help/start'), ('POST', f'/api/docker/containers/{container}/delete')]:
                with self.assertRaises(ValueError):
                    portal.docker_operation(method, path)

    def test_docker_request_limits_and_errors(self):
        with patch('portal.DockerConnection') as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = b'x' * 262145
            body, truncated = portal.docker_request('GET', '/containers/id/logs?tail=200')
            self.assertTrue(truncated)
            self.assertEqual(len(body), 262144)
            response.read.assert_called_with(262145)
            response.status = 404
            with self.assertRaises(RuntimeError):
                portal.docker_request('POST', '/containers/id/start')
            self.assertEqual(connection.return_value.close.call_count, 2)

    @unittest.skipUnless(os.name == 'posix', 'Installer check requires Linux / WSL')
    def test_installer_manages_docker_permission_and_preserves_config(self):
        source = Path(__file__).parent
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('portal.py', 'index.html', 'deploy/service-portal.service'):
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_bytes((source / name).read_bytes())
            script = (source / 'install.sh').read_text()
            script = script.replace('if [[ "${EUID}" -ne 0 ]]; then', 'if false; then')
            for original, replacement in [('/opt/service-portal', root / 'app'),
                                          ('/etc/service-portal', root / 'config'),
                                          ('/etc/systemd/system/service-portal', root / 'units/service-portal'),
                                          ('/var/run/docker.sock', root / 'docker.sock')]:
                script = script.replace(original, str(replacement))
            (root / 'install.sh').write_text(script)
            (root / 'units').mkdir()
            commands = root / 'bin'
            commands.mkdir()
            stubs = {
                'ss': 'exit 0',
                'sleep': 'exit 0',
                'apt-get': 'exit 99',
                'getent': 'if [ "$1" = group ] && [ "${TEST_MISSING_GROUP:-0}" = 1 ]; then exit 1; fi\nexit 0',
                'systemctl': 'printf "%s\\n" "$*" >> "$TEST_SYSTEMCTL_LOG"',
            }
            for name, command in stubs.items():
                executable = commands / name
                executable.write_text('#!/bin/sh\n' + command + '\n')
                executable.chmod(0o755)
            env = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ['PATH'],
                       TEST_SYSTEMCTL_LOG=str(root / 'systemctl.log'))
            self.cfg['docker_enabled'] = True
            (root / 'config.json').write_text(json.dumps(self.cfg))

            def install(**changes):
                return subprocess.run(['bash', str(root / 'install.sh')], env=dict(env, **changes),
                                      capture_output=True, text=True, timeout=20)

            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as docker_socket:
                docker_socket.bind(str(root / 'docker.sock'))
                result = install()
                self.assertEqual(result.returncode, 0, result.stderr)
                override = root / 'units/service-portal.service.d/90-service-portal-docker.conf'
                self.assertIn('SupplementaryGroups=docker', override.read_text())
                runtime_config = root / 'config/config.json'
                original_config = runtime_config.read_bytes()
                self.cfg['docker_enabled'] = False
                (root / 'config.json').write_text(json.dumps(self.cfg))
                self.assertEqual(install().returncode, 0)
                self.assertEqual(runtime_config.read_bytes(), original_config)
                self.assertTrue(override.exists())
                manual = override.with_name('docker.conf')
                manual.write_text('# administrator override\n')
                runtime_config.write_text(json.dumps(self.cfg))
                self.assertEqual(install().returncode, 0)
                self.assertFalse(override.exists())
                self.assertTrue(manual.exists())
                self.cfg['docker_enabled'] = True
                runtime_config.write_text(json.dumps(self.cfg))
                previous_log = (root / 'systemctl.log').read_bytes()
                result = install(TEST_MISSING_GROUP='1')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('docker 用户组不存在', result.stderr)
                self.assertEqual((root / 'systemctl.log').read_bytes(), previous_log)
            (root / 'docker.sock').unlink()
            result = install()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('请先启动 Docker Engine', result.stderr)
            self.assertEqual((root / 'systemctl.log').read_bytes(), previous_log)


if __name__ == '__main__':
    unittest.main()
