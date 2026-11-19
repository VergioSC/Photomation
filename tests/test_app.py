#!/usr/bin/env python3
"""Checks for the request gate: the password, and cross-site writes.
Run: .venv/bin/python tests/test_app.py"""

import importlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def load_app(password=""):
    """Import app.py with a given password, from scratch each time.

    PHOTOMATION_KEEP_BATCH so importing it does not move the real batch into
    the trash, which is what a plain import does by design.
    """
    os.environ["PHOTOMATION_KEEP_BATCH"] = "1"
    os.environ["PHOTOMATION_PASSWORD"] = password
    import config
    importlib.reload(config)
    import app as app_module
    importlib.reload(app_module)
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_without_a_password_everything_is_open():
    client = load_app("")
    assert client.get("/dashboard").status_code == 200


def test_with_a_password_every_route_is_closed():
    """Not just upload: a thumbnail is somebody's photograph too."""
    client = load_app("hunter2")
    for path in ("/", "/dashboard", "/grade", "/api/keepers", "/api/recipes"):
        assert client.get(path).status_code == 401, f"{path} was not protected"


def test_the_right_password_gets_in():
    client = load_app("hunter2")
    import base64
    token = base64.b64encode(b"photomation:hunter2").decode()
    reply = client.get("/dashboard", headers={"Authorization": f"Basic {token}"})
    assert reply.status_code == 200, reply.status_code


def test_the_wrong_password_does_not():
    client = load_app("hunter2")
    import base64
    for pair in (b"photomation:wrong", b"someone:hunter2", b"photomation:"):
        token = base64.b64encode(pair).decode()
        reply = client.get("/dashboard", headers={"Authorization": f"Basic {token}"})
        assert reply.status_code == 401, f"{pair!r} got in"


def test_health_is_the_one_route_without_a_password():
    """An orchestrator has no credentials and should not be given any. It
    must also stop naming the machine's folders once a password is set."""
    client = load_app("hunter2")
    reply = client.get("/api/health")
    assert reply.status_code == 200
    assert "input_dir" not in reply.get_json()


def test_a_cross_site_write_is_refused():
    """A plain HTML form posts across sites with no permission check, so any
    page you have open could fire POST /api/empty-trash at 127.0.0.1 and
    permanently delete every file the app has ever moved aside."""
    client = load_app("")
    for path in ("/api/empty-trash", "/api/new-batch"):
        reply = client.post(path, headers={"Sec-Fetch-Site": "cross-site"})
        assert reply.status_code == 403, f"{path} accepted a cross-site POST"


def test_the_page_itself_can_still_write():
    client = load_app("")
    reply = client.put("/api/edits", json={"edits": {}},
                       headers={"Sec-Fetch-Site": "same-origin"})
    assert reply.status_code == 200, reply.status_code


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {name}: {exc}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
