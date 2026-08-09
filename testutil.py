"""Shared safety guard for the test scripts that spin up their own cluster.

Every `test_*.py` here hardcodes ports 8001-8003 and `./node1.db` through
`./node3.db`, and cleans up by deleting those files. That is fine when the
test created them -- and destructive when it didn't, because they are the
exact paths the demo cluster uses. Two things go wrong, in this order:

1. The test doesn't fail to bind a busy port. uvicorn just loses the race
   and the test's requests land on the *already running* cluster, writing
   its synthetic events (guest `g1`, devices `deviceA`/`deviceB`) into real
   data. Confirmed live: a run left a fake guest sitting in the gallery.
2. Then the `finally` block deletes those databases on the way out.

Assertions fail confusingly first ("expected 3 aesthetic_score events"
counting 109 pre-existing ones), which reads like a code regression rather
than the environment problem it is.

So: refuse to start unless the ports are free and the database files don't
already exist. Both checks are about *not clobbering someone else's state*,
which is why this exits rather than trying to clean up on the caller's
behalf -- deleting the thing we're protecting would defeat the point.
"""
import os
import socket
import sys


def _port_busy(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


def ensure_safe_to_run(ports=(8001, 8002, 8003), db_paths=None) -> None:
    """Exit(2) with an actionable message unless it's safe to spin up a
    throwaway cluster on these ports and databases."""
    if db_paths is None:
        db_paths = [f"./node{i}.db" for i in range(1, len(ports) + 1)]

    busy = [p for p in ports if _port_busy(p)]
    existing = [p for p in db_paths if os.path.exists(p)]
    if not busy and not existing:
        return

    print("REFUSING TO RUN -- this test would damage state it did not create.\n")
    if busy:
        print(f"  Something is already listening on: {', '.join(map(str, busy))}")
        print("  This test would silently attach to it and write synthetic events")
        print("  into whatever data that cluster holds.\n")
    if existing:
        print(f"  These database files already exist: {', '.join(existing)}")
        print("  This test DELETES them when it finishes.\n")
    print("  Fix: stop the demo cluster and move the .db files aside, e.g.")
    print("       mkdir -p .dbbackup && mv node*.db .dbbackup/")
    print("  Restore them afterwards with: mv .dbbackup/node*.db .")
    sys.exit(2)
