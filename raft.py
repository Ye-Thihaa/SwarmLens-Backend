import asyncio
import os
import random
import time
import httpx

# NOTE on timing: the roadmap spec lists a 150-300ms election timeout and a
# 1s leader heartbeat. Taken literally that's backwards -- followers would
# time out and start a new election *before* the sitting leader's next
# heartbeat ever arrives, so the cluster would never settle. Raft requires
# heartbeat << election timeout. Kept the specified 150-300ms election
# timeout as-is; dropped the heartbeat cadence to 50ms so a stable leader
# actually stays elected between timeouts.
#
# These defaults assume LAN/loopback, where an RPC is sub-millisecond, and
# every number in ROADMAP.md's Phase 8 section was measured with them. They
# are env-overridable for one specific deployment shape: nodes separated by
# the public internet (three Render services, say), where a single RPC is
# 20-200ms and the 50ms budgets below cannot be met by a healthy peer. Left
# unchanged there, every RPC times out, every election round stalls, and
# the cluster churns leaders forever -- the exact livelock the RPC_TIMEOUT
# comment describes, triggered by latency instead of a dead peer.
#
# Keep the ratios, not just the values: heartbeat << election timeout, and
# RPC timeout well under election timeout. A workable internet-latency set
# is RAFT_HEARTBEAT=1.0, RAFT_ELECTION_MIN=3.0, RAFT_ELECTION_MAX=6.0,
# RAFT_RPC_TIMEOUT=1.0 -- 20x slower failover, which is the honest price of
# running a 50ms-tuned protocol across regions. Don't quietly retune these
# for a local run: Phase 8's numbers stop describing the system if you do.
ELECTION_TIMEOUT_RANGE = (
    float(os.getenv("RAFT_ELECTION_MIN", "0.150")),
    float(os.getenv("RAFT_ELECTION_MAX", "0.300")),
)
HEARTBEAT_INTERVAL = float(os.getenv("RAFT_HEARTBEAT", "0.05"))
# Must be well under the election timeout: asyncio.gather waits for every
# peer, including dead ones, so a slow per-RPC timeout (e.g. httpx's 5s
# default) makes every election round take that long end-to-end -- long
# enough for a competing candidate's vote request to land and overwrite
# this node's state before its own round even finishes. That produces a
# permanent livelock between two candidates, never converging on a leader.
RPC_TIMEOUT = float(os.getenv("RAFT_RPC_TIMEOUT", "0.05"))


class Raft:
    """Minimum-viable Raft: leader election and heartbeats only. No log
    replication -- the event log + gossip already handles replication,
    this just decides which single node is allowed to act."""

    def __init__(self, node_id: str, peers: list[str]):
        self.node_id = node_id
        self.peers = peers

        self.current_term = 0
        self.voted_for: str | None = None
        self.role = "follower"  # follower | candidate | leader
        self.leader_id: str | None = None
        self.election_deadline = 0.0

        self.running = False
        self._lock = asyncio.Lock()

    async def start(self):
        self.running = True
        self._reset_election_deadline()
        asyncio.create_task(self._loop())

    async def stop(self):
        self.running = False

    def _reset_election_deadline(self):
        timeout = random.uniform(*ELECTION_TIMEOUT_RANGE)
        self.election_deadline = time.monotonic() + timeout

    def _become_follower_locked(self, term: int):
        """Caller must hold self._lock."""
        self.current_term = term
        self.role = "follower"
        self.voted_for = None
        self.leader_id = None

    async def _loop(self):
        # trust_env=False: see the same note in gossip.py -- peers are
        # always 127.0.0.1, and a system proxy would silently break every
        # RPC here, not just add latency this timeout is already tight
        # against.
        async with httpx.AsyncClient(timeout=RPC_TIMEOUT, trust_env=False) as client:
            while self.running:
                if self.role == "leader":
                    await self._send_heartbeats(client)
                    await asyncio.sleep(HEARTBEAT_INTERVAL)
                else:
                    if time.monotonic() >= self.election_deadline:
                        await self._start_election(client)
                    else:
                        await asyncio.sleep(0.01)

    async def _start_election(self, client: httpx.AsyncClient):
        async with self._lock:
            self.current_term += 1
            self.role = "candidate"
            self.voted_for = self.node_id
            term = self.current_term
        self._reset_election_deadline()

        if not self.peers:
            # single-node "cluster": trivially wins its own election
            async with self._lock:
                if self.role == "candidate" and self.current_term == term:
                    self.role = "leader"
                    self.leader_id = self.node_id
            return

        needed = (len(self.peers) + 1) // 2 + 1  # majority of all N nodes

        async def request_vote(peer: str):
            try:
                r = await client.post(f"{peer}/raft/request_vote", json={
                    "term": term,
                    "candidate_id": self.node_id,
                })
                r.raise_for_status()
                return r.json()
            except Exception:
                return None

        results = await asyncio.gather(*(request_vote(p) for p in self.peers))

        votes = 1  # vote for self
        highest_term_seen = term
        for res in results:
            if not res:
                continue
            highest_term_seen = max(highest_term_seen, res.get("term", term))
            if res.get("granted"):
                votes += 1

        if highest_term_seen > term:
            await self._step_down(highest_term_seen)
            return

        async with self._lock:
            if self.role == "candidate" and self.current_term == term and votes >= needed:
                self.role = "leader"
                self.leader_id = self.node_id
        # Otherwise: split vote or overtaken. Stay candidate/follower and
        # let the (freshly randomized) election deadline trigger a retry.

    async def _send_heartbeats(self, client: httpx.AsyncClient):
        async def send(peer: str):
            try:
                r = await client.post(f"{peer}/raft/heartbeat", json={
                    "term": self.current_term,
                    "leader_id": self.node_id,
                })
                r.raise_for_status()
                body = r.json()
                if body.get("term", 0) > self.current_term:
                    await self._step_down(body["term"])
            except Exception:
                pass  # unreachable peer this round; retried next round

        await asyncio.gather(*(send(p) for p in self.peers))

    async def _step_down(self, term: int):
        async with self._lock:
            if term >= self.current_term:
                self._become_follower_locked(term)
        self._reset_election_deadline()

    # ---- RPC handlers, wired up in main.py ----

    async def handle_request_vote(self, term: int, candidate_id: str) -> dict:
        async with self._lock:
            if term > self.current_term:
                self._become_follower_locked(term)
            if term < self.current_term:
                return {"term": self.current_term, "granted": False}
            if self.voted_for in (None, candidate_id):
                self.voted_for = candidate_id
                granted = True
            else:
                granted = False
        if granted:
            self._reset_election_deadline()
        return {"term": self.current_term, "granted": granted}

    async def handle_heartbeat(self, term: int, leader_id: str) -> dict:
        async with self._lock:
            if term > self.current_term:
                self._become_follower_locked(term)
            if term < self.current_term:
                return {"term": self.current_term, "ok": False}
            self.role = "follower"
            self.leader_id = leader_id
            self.voted_for = leader_id
        self._reset_election_deadline()
        return {"term": self.current_term, "ok": True}

    def status(self) -> dict:
        return {
            "node": self.node_id,
            "role": self.role,
            "term": self.current_term,
            "leader_id": self.leader_id,
            "voted_for": self.voted_for,
        }
