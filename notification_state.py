"""Persist-before-send ledger. GitHub uses a dedicated branch and compare-and-swap."""
import base64
import json
import os
from pathlib import Path
from urllib.parse import quote

import requests

STATE_BRANCH = "notification-state"
STATE_PATH = "pushplus-state.json"


class StateError(RuntimeError):
    pass


class FileState:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        if not self.path.exists():
            return {"version": 1, "entries": {}}
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if state.get("version") != 1 or not isinstance(state.get("entries"), dict):
                raise ValueError("invalid schema")
            return state
        except (OSError, ValueError) as exc:
            raise StateError("Notification state is unreadable; refusing to resend") from None

    def save(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        try:
            temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, self.path)
        except OSError:
            raise StateError("Cannot persist notification state") from None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = self.path.with_suffix(".lock")
        try:
            self.handle = self.lock.open("x", encoding="utf-8")
        except FileExistsError:
            raise StateError("Another sender or stale lock exists; inspect before retrying") from None
        return self

    def __exit__(self, *args):
        self.handle.close()
        self.lock.unlink()


class GitHubState:
    def __init__(self, repository, token, session=None):
        if not repository or not token:
            raise StateError("GitHub state requires repository and GITHUB_TOKEN")
        self.repository = repository
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        self.base = f"https://api.github.com/repos/{repository}"
        self.sha = None

    def request(self, method, path, **kwargs):
        try:
            response = self.session.request(method, self.base + path, timeout=(5, 20), **kwargs)
        except requests.RequestException:
            raise StateError("GitHub notification-state request failed") from None
        if response.status_code not in (200, 201, 404):
            raise StateError(f"GitHub notification-state HTTP {response.status_code}; refusing an unsafe resend")
        return response

    def load(self):
        r = self.request("GET", f"/contents/{STATE_PATH}", params={"ref": STATE_BRANCH})
        if r.status_code == 404:
            self.sha = None
            return {"version": 1, "entries": {}}
        try:
            body = r.json()
            self.sha = body["sha"]
            state = json.loads(base64.b64decode(body["content"]).decode("utf-8"))
            if state.get("version") != 1 or not isinstance(state.get("entries"), dict):
                raise ValueError("invalid state schema")
            return state
        except (ValueError, KeyError, TypeError):
            raise StateError("GitHub notification state is invalid") from None

    def ensure_branch(self):
        branch = self.request("GET", f"/git/ref/heads/{STATE_BRANCH}")
        if branch.status_code == 200:
            return
        repo = self.request("GET", "").json()
        default = quote(repo["default_branch"], safe="")
        head = self.request("GET", f"/git/ref/heads/{default}").json()["object"]["sha"]
        self.request("POST", "/git/refs", json={"ref": f"refs/heads/{STATE_BRANCH}", "sha": head})

    def save(self, state):
        if self.sha is None:
            self.ensure_branch()
        body = {"message": "Persist notification delivery state", "branch": STATE_BRANCH, "content": base64.b64encode(json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")).decode("ascii")}
        if self.sha is not None:
            body["sha"] = self.sha
        # A 409/422 conflict fails before sending, rather than overwriting another sender.
        r = self.request("PUT", f"/contents/{STATE_PATH}", json=body)
        try:
            self.sha = r.json()["content"]["sha"]
        except (ValueError, KeyError, TypeError):
            raise StateError("GitHub did not confirm durable state storage") from None

    def __enter__(self):
        # Workflow concurrency serializes normal runs; content SHA also protects racing writers.
        return self

    def __exit__(self, *args):
        return False


def state_store(output_dir):
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return GitHubState(os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN"))
    return FileState(Path(output_dir) / "notification-state.json")
