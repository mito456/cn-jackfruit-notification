"""
cert_server.py
HTTP server for distributing server.crt to clients
Run: python cert_server.py
Access: http://localhost:8000/server.crt
"""
import http.server
import socketserver
import os

PORT = 8001
CERT_FILE = "server.crt"

class CertRequestHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/' or self.path == '/server.crt':
            if os.path.exists(CERT_FILE):
                self.send_response(200)
                self.send_header('Content-type', 'application/octet-stream')
                self.send_header('Content-Disposition', f'attachment; filename="{CERT_FILE}"')
                self.end_headers()
                with open(CERT_FILE, 'rb') as f:
                    self.wfile.write(f.read())
            else:
                self.send_error(404, f"{CERT_FILE} not found")
        else:
            self.send_error(404, "Not found")
    
    def log_message(self, format, *args):
        print(f"[HTTP] {format % args}")

if __name__ == "__main__":
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", PORT), CertRequestHandler) as httpd:
        print(f"🌐 Certificate server running on http://0.0.0.0:{PORT}")
        print(f"   Download: http://localhost:{PORT}/server.crt")
        print(f"   Press Ctrl+C to stop")
        httpd.serve_forever()
