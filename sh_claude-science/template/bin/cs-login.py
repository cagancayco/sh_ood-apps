#!/usr/bin/env python3

# hands out claude-science login links to the OnDemand session card
#
# claude-science authenticates a browser with a single-use link that expires a
# few minutes after it is minted, so there is no URL the "Connect to Claude
# Science" button could point at directly. This helper listens on its own port
# instead: every click asks the running daemon for a fresh link (the same thing
# `claude-science url` does, over the daemon's control socket) and sends the
# browser on to it, under the prefix OnDemand proxies the daemon at.
#
# Requests must carry ?token=<password>, matching $CS_LOGIN_TOKEN in the
# environment (set from the password generated in template/before.sh). Without
# it anybody able to reach this port could help themselves to a login link, and
# so to the session's files and credentials.

import argparse
import hmac
import html
import json
import os
import re
import subprocess
import sys

from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

# `claude-science url` prints one bare URL, of the form
# http://localhost:<port>/?nonce=<nonce>. Only the nonce is of use here: the
# host and port it names are the daemon's own, not the proxied ones.
NONCE_RE = re.compile(r"[?&]nonce=([A-Za-z0-9._~-]+)")

URL_TIMEOUT = 30


class LoginHandler(BaseHTTPRequestHandler):
    server_version = "cs-login/1.0"

    def do_GET(self):
        self.hand_off_to_daemon()

    def do_POST(self):
        # drain the body, so the browser sees a complete exchange
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.hand_off_to_daemon()

    def hand_off_to_daemon(self):
        token = parse_qs(urlparse(self.path).query).get("token", [""])[0]
        if not hmac.compare_digest(token, self.server.token):
            self.error_page(403, "Missing or invalid session token.")
            return

        try:
            url = subprocess.check_output(
                self.server.url_command,
                stderr=subprocess.STDOUT,
                timeout=URL_TIMEOUT,
            ).decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            self.error_page(504, "Timed out asking the daemon for a login link.")
            return
        except subprocess.CalledProcessError as err:
            output = err.output.decode("utf-8", "replace").strip()
            self.error_page(
                503,
                "The daemon didn't hand out a login link (exit {}): {}".format(
                    err.returncode, output
                ),
            )
            return

        nonce = NONCE_RE.search(url)
        if nonce is None:
            self.error_page(
                502, "Couldn't find a login nonce in what the daemon printed."
            )
            return

        self.login_page(
            "{}/?nonce={}".format(self.server.redirect_base, nonce.group(1))
        )

    def login_page(self, target):
        # The login URL goes in the body rather than a Location header. The
        # helper and the daemon sit under different /rnode prefixes, and Apache
        # rewrites any Location coming out of an /rnode backend to point back
        # inside that backend's own prefix -- a path, a relative reference and
        # even a full URL on the portal all come back here instead of reaching
        # the daemon. Response bodies it leaves alone, so the browser resolves
        # this path itself, against the portal page it is already on.
        attr = html.escape(target, quote=True)
        script = json.dumps(target).replace("<", "\\u003c")
        body = (
            "<!DOCTYPE html><html><head>"
            '<meta http-equiv="refresh" content="0; url={0}">'
            "<title>Claude Science</title></head>"
            "<body><script>location.replace({1})</script>"
            '<p>Signing you in&hellip; <a href="{0}">continue</a> '
            "if nothing happens.</p>"
            "</body></html>"
        ).format(attr, script)
        self.respond(200, body)

    def error_page(self, code, message):
        self.respond(code, (
            "<!DOCTYPE html><html><head><title>Claude Science</title></head>"
            "<body><h1>Can't sign you in</h1><p>{}</p>"
            "<p>Check the session output for details, then try Connect again.</p>"
            "</body></html>"
        ).format(html.escape(message)))

    def respond(self, code, body):
        encoded = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def log_request(self, code="-", size="-"):
        # log the path only: the query string carries the session token
        self.log_message('"%s %s" %s', self.command, urlparse(self.path).path, code)

    def log_message(self, fmt, *args):
        # flush as we go: stderr is block-buffered once it lands in the
        # session's output log, so otherwise these show up only at teardown
        sys.stderr.write("%s - - [%s] %s\n" % (
            self.address_string(), self.log_date_time_string(), fmt % args))
        sys.stderr.flush()


class LoginServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def parse_args(argv=sys.argv[1:]):
    parser = argparse.ArgumentParser(description="claude-science login helper")
    parser.add_argument("--port", type=int, required=True,
                        help="port to listen for Connect requests on")
    parser.add_argument("--redirect-base", required=True,
                        help="URL prefix the daemon is proxied under, "
                             "e.g. /rnode/sh03-01n01/45123")
    parser.add_argument("--data-dir",
                        help="claude-science data directory, if not the default")
    parser.add_argument("--claude-science", default="claude-science",
                        help="claude-science executable to ask for login links")
    return parser.parse_args(argv)


def main(argv=sys.argv[1:]):
    args = parse_args(argv)

    token = os.environ.get("CS_LOGIN_TOKEN")
    if not token:
        sys.exit("cs-login: $CS_LOGIN_TOKEN is not set, refusing to start")

    url_command = [args.claude_science, "url"]
    if args.data_dir:
        url_command += ["--data-dir", args.data_dir]

    server = LoginServer(("0.0.0.0", args.port), LoginHandler)
    server.token = token
    server.redirect_base = args.redirect_base.rstrip("/")
    server.url_command = url_command

    print("cs-login is listening on port {} -> {}/".format(
        args.port, server.redirect_base))
    sys.stdout.flush()

    server.serve_forever()


if __name__ == "__main__":
    main()
