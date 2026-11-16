from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socketserver
import threading
import unittest

from risk_pr_agent.github import GitHubClient, GitHubError


class LocalServer(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer.server_bind looks up the host's FQDN, which can take many seconds.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


class RedirectTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        test = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                test.requests.append((self.path, self.headers.get("Authorization")))
                if self.path.startswith("/repos/example/project/pulls/7"):
                    self.send_response(302)
                    self.send_header("Location", "/elsewhere")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = b'{"number": 7}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = LocalServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.api = f"http://127.0.0.1:{server.server_address[1]}"

    def test_redirect_is_an_error_and_the_token_is_not_resent(self):
        client = GitHubClient(token="github-test-key", api_url=self.api)
        with self.assertRaises(GitHubError):
            client.request_json("/repos/example/project/pulls/7")
        self.assertEqual(self.requests, [("/repos/example/project/pulls/7", "Bearer github-test-key")])

    def test_plain_response_still_reads(self):
        client = GitHubClient(token="github-test-key", api_url=self.api)
        self.assertEqual(client.request_json("/repos/example/project/issues/1"), {"number": 7})


if __name__ == "__main__":
    unittest.main()
