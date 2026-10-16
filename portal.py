#!/usr/bin/env python3
"""Discover local HTTP services on Ubuntu; Python 3.10+, no pip packages."""
import argparse
import concurrent.futures
import datetime
import http.client
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import ssl
import subprocess
import threading
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


class TitleParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == 'title':
            self.inside = True

    def handle_endtag(self, tag):
        if tag.lower() == 'title':
            self.inside = False

    def handle_data(self, data):
        if self.inside:
            self.parts.append(data)


def load_config(path):
    cfg = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    cfg.setdefault('docker_enabled', False)
    if not isinstance(cfg['docker_enabled'], bool):
        raise ValueError('docker_enabled must be boolean')
    for key, low, high in [('port', 1, 65535), ('scan_interval', 10, 86400),
                           ('probe_timeout', 0.2, 10), ('workers', 1, 64)]:
        value = cfg[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            raise ValueError('Invalid configuration: ' + key)
        if key in ('port', 'scan_interval', 'workers') and not isinstance(value, int):
            raise ValueError(key + ' must be an integer')
    ipaddress.ip_address(cfg['bind'])
    if not isinstance(cfg['include_loopback'], bool):
        raise ValueError('include_loopback must be boolean')
    if any(type(p) is not int or not 1 <= p <= 65535 for p in cfg['exclude_ports']):
        raise ValueError('Invalid exclude_ports')
    for port, entry in cfg['services'].items():
        if not port.isdecimal() or not 1 <= int(port) <= 65535:
            raise ValueError('Invalid service port')
        path = entry.get('path', '/')
        if not isinstance(path, str) or not path.startswith('/') or path.startswith('//') or '\\' in path or any(ord(c) < 32 for c in path):
            raise ValueError('Service path must be a local absolute URL path')
        if 'name' in entry and not isinstance(entry['name'], str):
            raise ValueError('Service name must be a string')
    return cfg


def parse_listeners(output):
    endpoints = set()
    for line in output.splitlines():
        columns = line.split()
        if len(columns) < 5:
            continue
        try:
            host, port = columns[3].rsplit(':', 1)
            host = host.strip('[]')
            # ss may print '*' for an unspecified address; probing IPv4 is sufficient here.
            address = ipaddress.ip_address('0.0.0.0' if host == '*' else host)
            port = int(port)
            if not 1 <= port <= 65535:
                continue
            target = ('::1' if address.version == 6 else '127.0.0.1') if address.is_unspecified else host
            endpoints.add((target, port, address.is_loopback))
        except ValueError:
            continue
    return sorted(endpoints)


def probe(endpoint, cfg):
    host, port, loopback = endpoint
    entry = cfg['services'].get(str(port), {})
    path = entry.get('path', '/')
    # No redirects: discovery must never follow a response to another host.
    # TLS first: HTTPS listeners may return HTTP 400 to a plaintext request.
    for scheme in ('https', 'http'):
        connection = None
        try:
            if scheme == 'https':
                # Local discovery accepts self-signed certificates. Browsers still validate them.
                connection = http.client.HTTPSConnection(host, port, timeout=cfg['probe_timeout'], context=ssl._create_unverified_context())
            else:
                connection = http.client.HTTPConnection(host, port, timeout=cfg['probe_timeout'])
            connection.request('GET', path, headers={'User-Agent': 'ServicePortal/1.0', 'Connection': 'close', 'Accept-Encoding': 'identity'})
            response = connection.getresponse()
            if not 100 <= response.status <= 599:
                continue
            title = ''
            if 'html' in response.getheader('Content-Type', '').lower():
                try:
                    raw = response.read1(32768)
                    parser = TitleParser()
                    parser.feed(raw.decode('utf-8', errors='replace'))
                    title = ' '.join(''.join(parser.parts).split())[:160]
                except (OSError, ValueError, http.client.HTTPException):
                    pass  # A valid HTTP response is enough, even if the body stalls.
            return dict(port=port, scheme=scheme, name=entry.get('name') or title or f'Web 服务 :{port}',
                        status=response.status, path=path, loopback=loopback, bind_address=host)
        except (OSError, ValueError, http.client.HTTPException):
            pass
        finally:
            if connection:
                connection.close()
    return None


def scan(cfg):
    result = subprocess.run(['ss', '-H', '-ltn'], capture_output=True, text=True, check=True, timeout=10)
    endpoints = [e for e in parse_listeners(result.stdout)
                 if e[1] != cfg['port'] and e[1] not in cfg['exclude_ports']
                 and (cfg['include_loopback'] or not e[2])]
    found = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=cfg['workers']) as pool:
        for item in pool.map(lambda e: probe(e, cfg), endpoints):
            if item:
                key = (item['port'], item['scheme'])
                if key not in found or (found[key]['loopback'] and not item['loopback']):
                    found[key] = item
    return sorted(found.values(), key=lambda item: (item['port'], item['scheme']))


class DockerConnection(http.client.HTTPConnection):
    def __init__(self):
        super().__init__('localhost', timeout=20)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect('/var/run/docker.sock')


def docker_request(method, path):
    connection = DockerConnection()
    try:
        connection.request(method, path, headers={'Content-Length': '0'})
        response = connection.getresponse()
        # Bound memory even when a container writes very long log lines.
        limit = 262144 if '/logs?' in path else 2097152
        body = response.read(limit + 1)
        if response.status >= 400:
            raise RuntimeError('Docker 请求失败（HTTP %s）' % response.status)
        if len(body) > limit and '/logs?' not in path:
            raise RuntimeError('Docker 返回数据过大，请通过主机命令查看。')
        return body[:limit], len(body) > limit
    finally:
        connection.close()


def docker_operation(method, path):
    if method == 'GET' and path == '/api/docker/containers':
        body, _ = docker_request('GET', '/containers/json?all=1')
        items = json.loads(body)
        return {'containers': [dict(id=item['Id'], name=', '.join(n.lstrip('/') for n in item.get('Names', [])) or item['Id'][:12],
                                    image=item.get('Image', ''), state=item.get('State', ''), status=item.get('Status', ''),
                                    ports=', '.join('%s:%s/%s' % (p['PublicPort'], p['PrivatePort'], p.get('Type', 'tcp'))
                                                    for p in item.get('Ports', []) if 'PublicPort' in p)) for item in items]}
    match = re.fullmatch(r'/api/docker/containers/([a-f0-9]{64})/(logs|start|stop|restart)', path)
    if not match or (method == 'GET') != (match[2] == 'logs'):
        raise ValueError('不支持的容器操作或容器 ID')
    container, action = match.groups()
    if action == 'logs':
        body, truncated = docker_request('GET', f'/containers/{container}/logs?stdout=1&stderr=1&tail=200&timestamps=1')
        # Non-TTY Docker logs have an 8-byte header per stdout/stderr frame.
        if len(body) >= 8 and body[0] in (0, 1, 2) and body[1:4] == b'\0\0\0':
            chunks, offset = [], 0
            while offset + 8 <= len(body):
                size = int.from_bytes(body[offset + 4:offset + 8], 'big')
                chunks.append(body[offset + 8:offset + 8 + size])
                offset += 8 + size
            body = b''.join(chunks)
        return {'logs': body.decode('utf-8', errors='replace'), 'truncated': truncated}
    docker_request('POST', f'/containers/{container}/{action}' + ('?t=10' if action != 'start' else ''))
    logging.info('Docker action=%s container=%s', action, container)
    return {'ok': True}


def refresh_state(cfg, state, lock):
    """One discovery round. A failed scan keeps the last successful result on screen."""
    try:
        services = scan(cfg)
        with lock:
            state.update(services=services, updated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(), error=None, scanning=False)
        logging.info('Discovered %s web services', len(services))
    except Exception:
        logging.exception('Discovery failed')
        with lock:
            state.update(error='端口发现失败，请管理员检查 journalctl 日志；当前显示上次成功结果。', scanning=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(Path(__file__).with_name('config.json')))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    cfg = load_config(args.config)
    docker_token = os.environ.get('SERVICE_PORTAL_DOCKER_TOKEN', '')
    if cfg['docker_enabled'] and (len(docker_token) < 32 or not docker_token.isascii() or any(c.isspace() for c in docker_token)):
        raise ValueError('Docker requires SERVICE_PORTAL_DOCKER_TOKEN: at least 32 ASCII characters, no whitespace')
    docker_lock = threading.Lock()
    state = dict(services=[], updated_at=None, error=None, scanning=True)
    lock = threading.Lock()

    def update():
        while True:
            refresh_state(cfg, state, lock)
            threading.Event().wait(cfg['scan_interval'])

    page = Path(__file__).with_name('index.html').read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path.startswith('/api/docker/'):
                self.handle_docker('GET', path)
                return
            if path in ('/', '/docker'):
                body, kind = page, 'text/html; charset=utf-8'
            elif path == '/api/services':
                with lock:
                    body = json.dumps(dict(state, scan_interval=cfg['scan_interval']), ensure_ascii=False).encode('utf-8')
                kind = 'application/json; charset=utf-8'
            elif path == '/healthz':
                body, kind = b'ok\n', 'text/plain; charset=utf-8'
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Frame-Options', 'DENY')
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            # No request bodies are accepted; close to avoid unread-body reuse.
            self.close_connection = True
            path = urlsplit(self.path).path
            if path.startswith('/api/docker/'):
                self.handle_docker('POST', path)
            else:
                self.send_error(404)

        def docker_json(self, status, data):
            body = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)

        def handle_docker(self, method, path):
            if not cfg['docker_enabled']:
                self.docker_json(403, {'error': 'Docker 运维尚未开启，请按 README 配置。'})
                return
            authorization = self.headers.get('Authorization', '').encode('utf-8')
            if not secrets.compare_digest(authorization, ('Bearer ' + docker_token).encode('utf-8')):
                self.docker_json(401, {'error': '访问令牌无效。'})
                return
            origin = self.headers.get('Origin')
            if origin and (urlsplit(origin).scheme not in ('http', 'https') or urlsplit(origin).netloc != self.headers.get('Host')):
                self.docker_json(403, {'error': '拒绝跨站请求。'})
                return
            if not docker_lock.acquire(blocking=False):
                self.docker_json(409, {'error': '另一项 Docker 请求正在执行，请稍后重试。'})
                return
            try:
                self.docker_json(200, docker_operation(method, path))
            except ValueError:
                self.docker_json(400, {'error': '不支持的容器操作或返回数据无效。'})
            except (OSError, http.client.HTTPException):
                self.docker_json(503, {'error': '无法连接 Docker，请检查 Docker 状态及 socket 权限。操作超时后请刷新确认容器状态。'})
            except RuntimeError as error:
                self.docker_json(502, {'error': str(error)})
            finally:
                docker_lock.release()

    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if ':' in cfg['bind'] else socket.AF_INET
        daemon_threads = True

    server = Server((cfg['bind'], cfg['port']), Handler)
    threading.Thread(target=update, daemon=True).start()
    logging.info('Serving on %s:%s', cfg['bind'], cfg['port'])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
