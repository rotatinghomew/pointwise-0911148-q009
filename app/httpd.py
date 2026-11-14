import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from app.repository import Conflict


def create_server(service, host='127.0.0.1', port=0):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def respond(self, status, payload):
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            route = urlsplit(self.path).path
            if route == '/health':
                self.respond(200, {'status': 'ok'})
            elif route == '/v1/catalog':
                self.respond(200, service.describe())
            elif route.startswith('/v1/analyses/'):
                try:
                    self.respond(200, service.repository.analysis(unquote(route.rsplit('/', 1)[1])))
                except KeyError as error:
                    self.respond(404, {'error': str(error)})
            else:
                self.respond(404, {'error': '地址不存在'})

        def do_POST(self):
            route = urlsplit(self.path).path
            if route not in {'/v1/analyses', '/v1/actions'}:
                self.respond(404, {'error': '地址不存在'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 2_000_000:
                    raise ValueError('请求大小无效')
                request = json.loads(self.rfile.read(length))
                result = service.run(request) if route.endswith('analyses') else service.apply(request)
                self.respond(200, result)
            except Conflict as error:
                self.respond(409, {'error': str(error)})
            except (ValueError, KeyError, TypeError) as error:
                self.respond(400, {'error': str(error)})

    return ThreadingHTTPServer((host, port), Handler)
