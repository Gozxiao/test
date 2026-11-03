"""Ephemeral synthetic TLS edge. MUST run only in the disposable GitHub runner."""
import http.server
import pathlib
import ssl
import urllib.request

FLAG = pathlib.Path('/tmp/r3b-stale-public')

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/assets/') or self.path == '/':
            if FLAG.exists() and self.path == '/':
                body=b'<!doctype html><html><body>INTENTIONALLY_STALE_SYNTHETIC</body></html>'
            else:
                try:
                    body=urllib.request.urlopen('http://127.0.0.1:8080'+self.path,timeout=10).read()
                except Exception:
                    self.send_error(502); return
            self.send_response(200)
            self.send_header('Content-Type','text/html' if self.path=='/' else 'application/javascript')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers();self.wfile.write(body)
        else:
            self.send_error(404)
    def log_message(self,*args):
        return

server=http.server.ThreadingHTTPServer(('127.0.0.1',443),H)
ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain('/tmp/r3b-tls/leaf.crt','/tmp/r3b-tls/leaf.key')
server.socket=ctx.wrap_socket(server.socket,server_side=True)
server.serve_forever()
