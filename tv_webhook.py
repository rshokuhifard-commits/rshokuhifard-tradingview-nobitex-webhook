from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import time

HOST = "0.0.0.0"
PORT = 8080

class Handler(BaseHTTPRequestHandler):

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")

            print("\n===== TRADINGVIEW WEBHOOK =====")
            print("TIME :", time.strftime("%Y-%m-%d %H:%M:%S"))
            print("BODY :", body)
            print("===============================\n")

            try:
                data = json.loads(body)
                print("ACTION :", data.get("action"))
                print("SYMBOL :", data.get("symbol"))
                print("PRICE  :", data.get("price"))
            except Exception:
                pass

            response = b'{"status":"ok"}'

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        except Exception as e:
            print("WEBHOOK ERROR:", e)
            self.send_response(500)
            self.end_headers()

    def do_GET(self):
        response = b"TradingView webhook server is running"

        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format, *args):
        pass

print("====================================")
print("TRADINGVIEW WEBHOOK SERVER")
print("Listening on 0.0.0.0:8080")
print("====================================")

HTTPServer((HOST, PORT), Handler).serve_forever()
