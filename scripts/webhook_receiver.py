import argparse
import hashlib
import hmac
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

SECRET = ""
MAX_SKEW_SECONDS = 300


def verify_signature(secret: str, timestamp: str, raw_body: bytes, signature: str) -> bool:
    if not secret:
        return True
    if not signature or not timestamp:
        return False
    try:
        if abs(time.time() - int(timestamp)) > MAX_SKEW_SECONDS:
            return False
    except ValueError:
        return False

    message = timestamp.encode("utf-8") + b"." + raw_body
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length)

        timestamp = self.headers.get("X-Webhook-Timestamp", "")
        signature = self.headers.get("X-Webhook-Signature", "")
        event = self.headers.get("X-Webhook-Event", "(none)")
        delivery = self.headers.get("X-Webhook-Delivery", "(none)")

        if not verify_signature(SECRET, timestamp, raw_body, signature):
            print(f"[거부] 서명 검증 실패: event={event} delivery={delivery}")
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"detail":"invalid signature"}')
            return

        try:
            body = json.loads(raw_body.decode("utf-8"))
            pretty = json.dumps(body, ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pretty = repr(raw_body[:500])

        print(f"\n[수신] event={event} delivery={delivery}\n{pretty}")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, fmt, *args):
        pass


def main():
    global SECRET
    parser = argparse.ArgumentParser(description="웹훅 수신자 테스트 서버")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--secret", default="", help="백엔드 WEBHOOK_SECRET과 동일한 값")
    args = parser.parse_args()

    SECRET = args.secret
    server = HTTPServer((args.host, args.port), WebhookHandler)
    print(f"웹훅 수신 대기: http://{args.host}:{args.port}/webhook  (서명검증={'ON' if SECRET else 'OFF'})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")
        server.server_close()


if __name__ == "__main__":
    main()
