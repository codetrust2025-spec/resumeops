import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from resume_bot.ollama import Ollama


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        assert self.path=='/api/generate'
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        assert body['model']=='qwen2.5:7b'
        assert body['stream'] is False
        assert body['format']=='json'
        data=json.dumps({'response':json.dumps({'edits':['hello']})}).encode()
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def log_message(self,*args):pass


class APITests(unittest.TestCase):
    def test_ollama_generate_http_contract(self):
        http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=http.serve_forever,daemon=True)
        thread.start()
        try:
            client=Ollama('qwen2.5:7b',f'http://127.0.0.1:{http.server_port}',timeout=5)
            self.assertEqual(client.generate('test JSON'),{'edits':['hello']})
        finally:
            http.shutdown();http.server_close();thread.join()
