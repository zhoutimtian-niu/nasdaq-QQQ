"""Explicit storage-only self-check. Never imports or invokes a PushPlus sender."""
import os
import uuid

from notification_state import GitHubState, StateError


def check_state(store):
    marker = "storage-self-check-" + uuid.uuid4().hex
    with store:
        state = store.load()
        state["entries"][marker] = {"status": "self_test"}
        store.save(state)
        confirmed = store.load()
        if confirmed["entries"].get(marker) != {"status": "self_test"}:
            raise StateError("Persisted self-check marker could not be read back")
        del confirmed["entries"][marker]
        store.save(confirmed)
        final = store.load()
        if marker in final["entries"]:
            raise StateError("Storage self-check cleanup was not confirmed")
    print("PASS: persistent notification ledger write/read/cleanup; no PushPlus request made")


if __name__ == "__main__":
    check_state(GitHubState(os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")))
