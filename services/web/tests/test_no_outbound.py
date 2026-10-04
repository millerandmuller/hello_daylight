"""The promise: nothing leaves this app to anyone. There is no way out except the clipboard and the owner's inbox.

Four checks that fail when someone adds a way out: the imports, the HTTP verbs the code uses, the routes, the tools.
"""

import ast
import pathlib
import re

from app import main, tools

APP = pathlib.Path(main.HERE)
SOURCES = {p.name: p.read_text() for p in APP.glob("*.py")}

SENDERS = {"smtplib", "imaplib", "sendgrid", "mailgun", "twilio", "slack_sdk", "slack", "tweepy", "telegram", "discord", "praw", "linkedin", "atproto", "mastodon", "yagmail", "boto3", "postmarker"}
WRITE_VERBS = {"post", "put", "patch", "delete", "sendmail", "send_message", "send_mail", "sendall"}


def _imports(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module.split(".")[0]


def test_no_module_imports_a_way_to_send_something():
    for name, src in SOURCES.items():
        found = set(_imports(ast.parse(src))) & SENDERS
        assert not found, f"{name} imports {found}"


def test_outgoing_http_is_get_only_except_the_one_job_start_call():
    """`x.post(...)` on anything but the FastAPI app decorator is a write to somewhere. One is allowed: starting our own Cloud Run Job.
    (`delete` on a Firestore document in repo_firestore.py is a database call, not HTTP to a third party.)"""
    offenders = []
    for name, src in SOURCES.items():
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in WRITE_VERBS:
                base = node.func.value
                if isinstance(base, ast.Name) and base.id == "app":
                    continue  # a route decorator
                if name == "repo_firestore.py" and node.func.attr == "delete":
                    continue
                offenders.append((name, node.func.attr))
    assert offenders == [("launcher.py", "post")], offenders


def test_the_launcher_only_talks_to_our_own_job():
    src = SOURCES["launcher.py"]
    urls = re.findall(r"https?://[^\s\"'{}]+", src)
    assert all(u.startswith(("https://run.googleapis.com", "https://www.googleapis.com/auth")) for u in urls), urls


def test_every_http_request_the_tools_and_the_checks_make_is_a_get():
    web = SOURCES["web.py"]
    assert re.findall(r'\.stream\("(\w+)"', web) == ["GET"]
    assert ".post(" not in web and ".request(" not in web
    kc = SOURCES["keycheck.py"]
    assert ".get(" in kc and ".post(" not in kc


def test_routes_offer_no_way_out():
    for route in main.app.routes:
        path = getattr(route, "path", "")
        assert not re.search(r"send|mail|publish|deliver|dm\b|tweet|webhook", path, re.I), path
    posts = sorted(r.path for r in main.app.routes if "POST" in getattr(r, "methods", ()))
    public = [p for p in posts if p.startswith("/api/public/")]
    assert public == ["/api/public/intake", "/api/public/key-check", "/api/public/run"]
    # sign copies to the clipboard (in the browser) and marks the draft; it contacts no one
    sign = SOURCES["main.py"][SOURCES["main.py"].index("def card_action"): SOURCES["main.py"].index("def inbox")]
    assert "httpx" not in sign and "smtp" not in sign.lower() and "launch" not in sign


def test_a_scout_has_read_tools_only():
    names = set(tools.TOOL_NAMES)
    assert names <= {"web_search", "hn_search", "github_search", "rss_read", "read_page", "bluesky_search"}
    assert not any(re.search(r"post|send|write|submit|reply|dm|mail|login", n) for n in names)


def test_the_browser_script_never_sends_a_draft_anywhere():
    js = (APP / "static" / "public.js").read_text()
    fetches = re.findall(r'(?:api|fetch)\("([^"]+)"', js)
    assert set(fetches) <= {"/api/public/key-check", "/api/public/intake", "/api/public/run"}, fetches
    assert "mailto:" not in js and "window.open" not in js and "sendBeacon" not in js
    desk = (APP / "static" / "desk.js").read_text()
    assert "fetch(" not in desk and "XMLHttpRequest" not in desk and "sendBeacon" not in desk
