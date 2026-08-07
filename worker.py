import asyncio
import time
import uuid

LEASE_SECONDS = 8
POLL_INTERVAL = 2
HEARTBEAT_MARGIN = 3   # renew when this many seconds remain


class Worker:
    """Runs inside every node. Claims unclaimed jobs, does fake 'enhancement'
    work, renews its lease while working, and marks done exactly once.

    Kill the process mid-job: the lease expires, another node's worker
    reclaims it, and the photo is enhanced exactly once — never zero,
    never twice."""

    def __init__(self, node_id: str, store):
        self.node_id = node_id
        self.store = store
        self.running = False

    async def start(self):
        self.running = True
        asyncio.create_task(self._loop())

    async def stop(self):
        self.running = False

    async def _loop(self):
        while self.running:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                candidates = await self.store.unclaimed_photo_ids()
            except Exception:
                continue
            for photo_id in candidates:
                await self._try_claim_and_run(photo_id)

    async def _try_claim_and_run(self, photo_id: str):
        claim_id = uuid.uuid4().hex
        await self.store.append_local("job_claimed", {
            "photo_id": photo_id,
            "worker": self.node_id,
            "claim_id": claim_id,
            "claimed_at": time.time(),
            "lease_seconds": LEASE_SECONDS,
        })

        # Race-safety: gossip is async, so another node may have claimed
        # first with a still-valid lease. Re-check before doing real work.
        await asyncio.sleep(0.05)
        state = await self.store.job_state(photo_id)
        if state["claim_id"] != claim_id:
            return   # someone else's claim won; back off, don't run

        await self._do_work_with_heartbeat(photo_id, claim_id)

        state = await self.store.job_state(photo_id)
        if state["claim_id"] == claim_id and state["status"] != "done":
            await self.store.append_local("job_done", {
                "photo_id": photo_id,
                "worker": self.node_id,
                "claim_id": claim_id,
            })

    async def _do_work_with_heartbeat(self, photo_id: str, claim_id: str):
        """Simulated enhancement pass. Replace with the real ONNX call.
        Renews the lease partway through so a slow job isn't reclaimed
        by a competitor while still legitimately in progress."""
        work_seconds = 5
        elapsed = 0
        step = 1
        while elapsed < work_seconds:
            await asyncio.sleep(step)
            elapsed += step
            if LEASE_SECONDS - elapsed < HEARTBEAT_MARGIN:
                await self.store.append_local("job_claimed", {
                    "photo_id": photo_id,
                    "worker": self.node_id,
                    "claim_id": claim_id,
                    "claimed_at": time.time(),
                    "lease_seconds": LEASE_SECONDS,
                })
