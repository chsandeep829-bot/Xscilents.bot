import os
import json
import urllib.request
from http.server import BaseHTTPRequestHandler

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)

        try:
            data = json.loads(post_data.decode('utf-8'))
            if "message" in data:
                chat_id = data["message"]["chat"]["id"]
                text = data["message"].get("text", "")

                # Send a reply back via Telegram API
                token = os.environ.get("TELEGRAM_BOT_TOKEN")
                url = f"https://api.telegram.org/bot{token}/sendMessage"
                payload = json.dumps({"chat_id": chat_id, "text": f"Echo: {text}"}).encode("utf-8")

                req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req)
        except Exception as e:
            print(f"Error: {e}")

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({"status": "success"}).encode('utf-8'))
