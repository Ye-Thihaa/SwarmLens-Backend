import asyncio
import os
import random
import time
import httpx

# Per-round HTTP budget. 3s is generous on a LAN, where a round is a digest
# exchange plus at most MAX_SYNC_BYTES of events. Env-overridable for the
# cross-region case (nodes on separate hosts), where the same round pays
# real RTT and TLS on top -- unlike raft's timers, nothing here is
# correctness-critical, a slow round just delays convergence by one
# interval. See raft.py's timing note for the deployment shape this is for.
RPC_TIMEOUT = float(os.getenv("GOSSIP_RPC_TIMEOUT", "3.0"))


class Gossip:
    """Periodic anti-entropy. Every round: pick one random peer, pull what we
    lack, push what they lack. Convergence is epidemic, not broadcast."""

    def __init__(self, node_id: str, peers: list[str], store, interval: float = 1.0):
        self.node_id = node_id
        self.peers = peers
        self.store = store
        self.interval = interval
        self.running = False
        self.partitioned: set[str] = set()   # chaos control
        self.peer_state: dict[str, dict] = {
            p: {"reachable": None, "last_ok": None, "fails": 0} for p in peers
        }
        self.rounds = 0
        self.events_pulled = 0
        self.events_pushed = 0

    async def start(self):
        self.running = True
        asyncio.create_task(self._loop())

    async def stop(self):
        self.running = False

    async def _loop(self):
        # trust_env=False: every peer is 127.0.0.1 -- httpx defaults to
        # honoring HTTP_PROXY/system proxy settings, which on a machine
        # with one configured routes loopback traffic through it too,
        # producing 502s / connection resets for purely local traffic
        # that was never meant to leave the machine.
        async with httpx.AsyncClient(timeout=RPC_TIMEOUT, trust_env=False) as client:
            while self.running:
                await asyncio.sleep(self.interval)
                if not self.peers:
                    continue
                peer = random.choice(self.peers)
                if peer in self.partitioned:
                    self._mark_fail(peer)
                    continue
                try:
                    await self._sync_with(client, peer)
                    self._mark_ok(peer)
                except Exception:
                    self._mark_fail(peer)
                self.rounds += 1

    async def _sync_with(self, client: httpx.AsyncClient, peer: str):
        my_digest = await self.store.digest()

        # PULL: hand over our digest, receive everything we are behind on
        r = await client.post(f"{peer}/gossip/sync", json={
            "from": self.node_id,
            "digest": my_digest,
        })
        r.raise_for_status()
        body = r.json()

        pulled = await self.store.merge_remote(body["events"])
        self.events_pulled += pulled

        # PUSH: their digest came back with the response, so we know their gaps
        outgoing = await self.store.events_missing_from(body["digest"])
        if outgoing:
            await client.post(f"{peer}/gossip/push", json={
                "from": self.node_id,
                "events": outgoing,
            })
            self.events_pushed += len(outgoing)

    def _mark_ok(self, peer):
        s = self.peer_state[peer]
        s["reachable"] = True
        s["last_ok"] = time.time()
        s["fails"] = 0

    def _mark_fail(self, peer):
        s = self.peer_state[peer]
        s["fails"] += 1
        if s["fails"] >= 3:          # simple phi-free failure detector
            s["reachable"] = False

    def alive_peers(self) -> list[str]:
        return [p for p, s in self.peer_state.items() if s["reachable"] is not False]

    def stats(self) -> dict:
        return {
            "rounds": self.rounds,
            "events_pulled": self.events_pulled,
            "events_pushed": self.events_pushed,
            "peers": self.peer_state,
            "partitioned": sorted(self.partitioned),
        }
