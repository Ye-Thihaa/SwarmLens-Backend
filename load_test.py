"""Phase 8 load test: fires real traffic at a real cluster (no mocks, no
locust -- plain asyncio + httpx, matching the existing dependency set) and
measures the numbers ROADMAP.md asks for:

  1. p50/p95/p99 upload + like latency (200 uploads, 500 likes, random
     node each)
  2. convergence time -- max digest lag sampled every 100ms after the
     load burst, until it hits 0
  3. recovery time after a node kill -- time from kill to job reclaim to
     job_done for an in-flight job
  4. leader re-election latency -- 20 trials, a fresh 3-node cluster each
     time (so no trial's dead node affects the next trial's baseline)
  5. throughput at N=1,2,3,4 nodes -- fixed upload count, wall-clock time
  6. gossip bandwidth per round -- real HTTP payload size of a
     /gossip/sync exchange, cold (full backlog) vs. warm (steady state)

Run against the real cluster this script spins up itself (never a mock).
Outputs raw JSON/CSV + matplotlib PNGs into ./load_test_results/
(gitignored -- results from one run on one machine, not source).

`python load_test.py --quick` runs a fast, reduced-scale smoke test of
every measurement (useful for validating the script itself); omit --quick
for the full spec'd numbers, which takes several minutes end to end.
"""
import asyncio
import json
import os
import random
import subprocess
import sys
import time

import httpx
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "load_test_results")
BASE_PORT = 8001


# ---------------------------- cluster helpers ----------------------------

def node_config(n: int):
    ports = [BASE_PORT + i for i in range(n)]
    configs = []
    for i, port in enumerate(ports):
        peers = ",".join(f"http://127.0.0.1:{p}" for p in ports if p != port)
        configs.append({"node_id": f"node{i + 1}", "port": port, "peers": peers})
    return configs


def start_cluster(configs, gossip_interval="1.0", db_suffix="_load"):
    procs = {}
    for c in configs:
        env = os.environ.copy()
        env["NODE_ID"] = c["node_id"]
        env["DB_PATH"] = f"./{c['node_id']}{db_suffix}.db"
        env["SELF_URL"] = f"http://127.0.0.1:{c['port']}"
        env["PEERS"] = c["peers"]
        env["GOSSIP_INTERVAL"] = gossip_interval
        procs[c["node_id"]] = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--port", str(c["port"])],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    return procs


def stop_cluster(procs, configs, db_suffix="_load"):
    for p in procs.values():
        p.kill()
    for p in procs.values():
        try:
            p.wait(timeout=5)
        except Exception:
            pass
    for c in configs:
        try:
            os.remove(f"./{c['node_id']}{db_suffix}.db")
        except FileNotFoundError:
            pass


async def wait_for_boot(configs, timeout=10):
    deadline = time.time() + timeout
    async with httpx.AsyncClient() as client:
        while time.time() < deadline:
            try:
                await asyncio.gather(*(
                    client.get(f"http://127.0.0.1:{c['port']}/health", timeout=1.0) for c in configs
                ))
                return True
            except Exception:
                await asyncio.sleep(0.2)
    return False


def percentiles(data, ps=(50, 95, 99)):
    data = sorted(data)
    out = {}
    for p in ps:
        k = (len(data) - 1) * p / 100
        f, c = int(k), min(int(k) + 1, len(data) - 1)
        out[f"p{p}"] = round(data[f] + (data[c] - data[f]) * (k - f), 4)
    return out


# ---------------------------- measurements ----------------------------

async def measure_latency(configs, n_photos, n_likes):
    ports = [c["port"] for c in configs]
    async with httpx.AsyncClient(timeout=15.0) as client:
        async def upload_one(i):
            port = random.choice(ports)
            t0 = time.perf_counter()
            r = await client.post(f"http://127.0.0.1:{port}/photos", json={
                "guest_id": f"g{i}", "zone": f"zone_{i % 10}", "composition_score": 80,
            })
            r.raise_for_status()
            return time.perf_counter() - t0, r.json()["photo_id"]

        results = await asyncio.gather(*(upload_one(i) for i in range(n_photos)))
        photo_latencies = [r[0] for r in results]
        photo_ids = [r[1] for r in results]

        async def like_one(i):
            port = random.choice(ports)
            pid = random.choice(photo_ids)
            t0 = time.perf_counter()
            r = await client.post(f"http://127.0.0.1:{port}/likes", json={
                "guest_id": f"liker{i}", "photo_id": pid,
            })
            r.raise_for_status()
            return time.perf_counter() - t0

        like_latencies = list(await asyncio.gather(*(like_one(i) for i in range(n_likes))))

    return photo_latencies, like_latencies, photo_ids


async def measure_convergence(configs, sample_interval=0.1, timeout=20):
    samples = []
    t0 = time.perf_counter()
    async with httpx.AsyncClient(timeout=2.0) as client:
        while time.perf_counter() - t0 < timeout:
            try:
                resps = await asyncio.gather(*(
                    client.get(f"http://127.0.0.1:{c['port']}/health") for c in configs
                ))
                digs = [r.json()["digest"] for r in resps]
            except Exception:
                await asyncio.sleep(sample_interval)
                continue
            origins = set()
            for d in digs:
                origins.update(d.keys())
            lag = 0
            for o in origins:
                vals = [d.get(o, 0) for d in digs]
                lag = max(lag, max(vals) - min(vals))
            elapsed = time.perf_counter() - t0
            samples.append((round(elapsed, 3), lag))
            if lag == 0:
                break
            await asyncio.sleep(sample_interval)
    return samples


async def measure_recovery(configs, procs):
    """Post a photo, wait for a node's worker to claim its job, kill that
    node, measure time until a surviving node reclaims + completes it."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.post(f"http://127.0.0.1:{configs[0]['port']}/photos", json={
            "guest_id": "loadtest", "zone": "recovery_test", "composition_score": 80,
        })
        photo_id = r.json()["photo_id"]

        # Poll every node directly and in parallel, not just one observer:
        # a node's own writes are visible to itself with zero gossip lag,
        # but visible to an observer only after the next gossip round
        # (up to GOSSIP_INTERVAL). Querying only one node risks detecting
        # the claim late enough that the claimant finishes its own 5s
        # fake-work window before the kill signal lands -- racing the
        # measurement against the very thing it's trying to interrupt,
        # instead of a clean "kill mid-work" scenario.
        claimant = None
        deadline = time.time() + 10
        while time.time() < deadline and not claimant:
            results = await asyncio.gather(*(
                client.get(f"http://127.0.0.1:{c['port']}/jobs/{photo_id}") for c in configs
            ), return_exceptions=True)
            for res in results:
                if isinstance(res, Exception):
                    continue
                js = res.json()
                if js["status"] == "claimed":
                    claimant = js["worker"]
                    break
            if not claimant:
                await asyncio.sleep(0.05)
        if not claimant:
            return None

        t_kill = time.perf_counter()
        procs[claimant].kill()
        procs[claimant].wait(timeout=5)
        del procs[claimant]

        survivor_port = next(c["port"] for c in configs if c["node_id"] != claimant)
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                js = (await client.get(f"http://127.0.0.1:{survivor_port}/jobs/{photo_id}")).json()
                if js["status"] == "done":
                    return time.perf_counter() - t_kill
            except Exception:
                pass
            await asyncio.sleep(0.2)
    return None


async def measure_election_latency(trials, gossip_interval="1.0"):
    configs = node_config(3)
    latencies = []
    for i in range(trials):
        procs = start_cluster(configs, gossip_interval=gossip_interval)
        try:
            if not await wait_for_boot(configs):
                continue
            async with httpx.AsyncClient(timeout=2.0) as client:
                leader_id = None
                deadline = time.time() + 10
                while time.time() < deadline and not leader_id:
                    for c in configs:
                        try:
                            r = await client.get(f"http://127.0.0.1:{c['port']}/raft/status")
                            if r.json()["role"] == "leader":
                                leader_id = c["node_id"]
                                break
                        except Exception:
                            pass
                    if not leader_id:
                        await asyncio.sleep(0.05)
                if not leader_id:
                    continue

                t0 = time.perf_counter()
                procs[leader_id].kill()
                procs[leader_id].wait(timeout=5)
                del procs[leader_id]

                new_leader = None
                deadline = time.time() + 5
                while time.time() < deadline and not new_leader:
                    for c in configs:
                        if c["node_id"] == leader_id:
                            continue
                        try:
                            r = await client.get(f"http://127.0.0.1:{c['port']}/raft/status")
                            if r.json()["role"] == "leader":
                                new_leader = c["node_id"]
                                break
                        except Exception:
                            pass
                    if not new_leader:
                        await asyncio.sleep(0.01)
                if new_leader:
                    latencies.append(time.perf_counter() - t0)
        finally:
            stop_cluster(procs, configs)
        print(f"    election trial {i + 1}/{trials}: "
              f"{latencies[-1]:.3f}s" if latencies and len(latencies) == i + 1 else f"    election trial {i + 1}/{trials}: FAILED")
    return latencies


async def measure_throughput(n, n_uploads):
    configs = node_config(n)
    procs = start_cluster(configs, db_suffix=f"_load_n{n}")
    try:
        if not await wait_for_boot(configs):
            return None
        ports = [c["port"] for c in configs]
        async with httpx.AsyncClient(timeout=15.0) as client:
            async def upload(i):
                port = ports[i % len(ports)]
                r = await client.post(f"http://127.0.0.1:{port}/photos", json={
                    "guest_id": f"g{i}", "zone": f"zone_{i % 10}", "composition_score": 80,
                })
                r.raise_for_status()

            t0 = time.perf_counter()
            await asyncio.gather(*(upload(i) for i in range(n_uploads)))
            elapsed = time.perf_counter() - t0
        return n_uploads / elapsed
    finally:
        stop_cluster(procs, configs, db_suffix=f"_load_n{n}")


async def measure_gossip_bandwidth(configs):
    async with httpx.AsyncClient(timeout=10.0) as client:
        r_cold = await client.post(f"http://127.0.0.1:{configs[0]['port']}/gossip/sync", json={"digest": {}})
        cold_bytes = len(r_cold.content)

        my_digest = (await client.get(f"http://127.0.0.1:{configs[1]['port']}/health")).json()["digest"]
        r_warm = await client.post(f"http://127.0.0.1:{configs[0]['port']}/gossip/sync", json={"digest": my_digest})
        warm_bytes = len(r_warm.content)
    return cold_bytes, warm_bytes


# ---------------------------- graphing ----------------------------

def save_graphs(results):
    os.makedirs(RESULTS_DIR, exist_ok=True)

    fig, ax = plt.subplots(figsize=(7, 4))
    ax.hist(results["photo_latencies"], bins=30, alpha=0.6, label="photo upload")
    ax.hist(results["like_latencies"], bins=30, alpha=0.6, label="like")
    for p, v in results["photo_latency_percentiles"].items():
        ax.axvline(v, color="C0", linestyle="--", linewidth=1)
    ax.set_xlabel("latency (s)"); ax.set_ylabel("count"); ax.set_title("Upload / like latency")
    ax.legend()
    fig.tight_layout(); fig.savefig(os.path.join(RESULTS_DIR, "latency.png")); plt.close(fig)

    if results["convergence_samples"]:
        xs, ys = zip(*results["convergence_samples"])
        fig, ax = plt.subplots(figsize=(7, 4))
        ax.plot(xs, ys, marker="o", markersize=3)
        ax.set_xlabel("time since load burst ended (s)"); ax.set_ylabel("max digest lag (events)")
        ax.set_title(f"Convergence after {len(results['photo_latencies'])} uploads + "
                     f"{len(results['like_latencies'])} likes")
        fig.tight_layout(); fig.savefig(os.path.join(RESULTS_DIR, "convergence.png")); plt.close(fig)

    if results["election_latencies"]:
        fig, ax = plt.subplots(figsize=(7, 4))
        ax.hist(results["election_latencies"], bins=15)
        ax.axvline(sum(results["election_latencies"]) / len(results["election_latencies"]),
                   color="red", linestyle="--", label="mean")
        ax.set_xlabel("re-election latency (s)"); ax.set_ylabel("trials")
        ax.set_title(f"Leader re-election latency ({len(results['election_latencies'])} trials)")
        ax.legend()
        fig.tight_layout(); fig.savefig(os.path.join(RESULTS_DIR, "election_latency.png")); plt.close(fig)

    if results["throughput"]:
        ns = sorted(int(k) for k in results["throughput"])
        vals = [results["throughput"][str(n)] for n in ns]
        fig, ax = plt.subplots(figsize=(6, 4))
        ax.bar([str(n) for n in ns], vals, color="C2")
        ax.set_xlabel("cluster size (N)"); ax.set_ylabel("uploads / sec")
        ax.set_title("Throughput vs. cluster size")
        fig.tight_layout(); fig.savefig(os.path.join(RESULTS_DIR, "throughput.png")); plt.close(fig)

    if results.get("gossip_bandwidth"):
        cold, warm = results["gossip_bandwidth"]["cold_bytes"], results["gossip_bandwidth"]["warm_bytes"]
        fig, ax = plt.subplots(figsize=(5, 4))
        ax.bar(["cold (full backlog)", "warm (steady state)"], [cold, warm], color=["C3", "C4"])
        ax.set_ylabel("bytes")
        ax.set_title("Gossip /sync payload size")
        fig.tight_layout(); fig.savefig(os.path.join(RESULTS_DIR, "gossip_bandwidth.png")); plt.close(fig)

    if results["recovery_seconds"] is not None:
        fig, ax = plt.subplots(figsize=(4, 4))
        ax.bar(["recovery"], [results["recovery_seconds"]], color="C5")
        ax.set_ylabel("seconds")
        ax.set_title("Node-kill -> job reclaim -> job_done")
        fig.tight_layout(); fig.savefig(os.path.join(RESULTS_DIR, "recovery.png")); plt.close(fig)


# ---------------------------- main ----------------------------

async def main():
    quick = "--quick" in sys.argv
    n_photos = 20 if quick else 200
    n_likes = 50 if quick else 500
    election_trials = 3 if quick else 20
    throughput_uploads = 30 if quick else 100

    os.makedirs(RESULTS_DIR, exist_ok=True)
    results = {}

    print(f"=== 1-2. latency / convergence / gossip bandwidth "
          f"({'quick' if quick else 'full'} mode) ===")
    configs3 = node_config(3)
    procs3 = start_cluster(configs3)
    try:
        if not await wait_for_boot(configs3):
            print("FAIL: 3-node cluster never booted"); return 1

        photo_lat, like_lat, photo_ids = await measure_latency(configs3, n_photos, n_likes)
        results["photo_latencies"] = photo_lat
        results["like_latencies"] = like_lat
        results["photo_latency_percentiles"] = percentiles(photo_lat)
        results["like_latency_percentiles"] = percentiles(like_lat)
        print(f"  photo upload p50/p95/p99: {results['photo_latency_percentiles']}")
        print(f"  like p50/p95/p99: {results['like_latency_percentiles']}")

        samples = await measure_convergence(configs3)
        results["convergence_samples"] = samples
        results["convergence_seconds"] = samples[-1][0] if samples and samples[-1][1] == 0 else None
        print(f"  convergence time: {results['convergence_seconds']}s "
              f"({len(samples)} samples, final lag {samples[-1][1] if samples else '?'})")

        cold, warm = await measure_gossip_bandwidth(configs3)
        results["gossip_bandwidth"] = {"cold_bytes": cold, "warm_bytes": warm}
        print(f"  gossip /sync payload: cold={cold}B warm={warm}B")
    finally:
        stop_cluster(procs3, configs3)

    # Recovery gets its own dedicated, otherwise-idle cluster -- not the
    # one above. Worker throughput is ~1 job/5s per node, so running this
    # right after a 200+500 burst means the recovery-test photo queues
    # behind dozens of still-processing jobs, starving the claim-detection
    # deadline with backlog contention that has nothing to do with the
    # actual kill -> reclaim -> done latency this is trying to isolate.
    print("=== 3. recovery after a node kill (dedicated idle cluster) ===")
    configs_r = node_config(3)
    procs_r = start_cluster(configs_r, db_suffix="_load_recovery")
    try:
        if await wait_for_boot(configs_r):
            recovery = await measure_recovery(configs_r, procs_r)
        else:
            recovery = None
        results["recovery_seconds"] = recovery
        print(f"  recovery (kill -> reclaim -> done): {recovery}s" if recovery is not None
              else "  recovery: FAILED (job never claimed or never finished)")
    finally:
        stop_cluster(procs_r, configs_r, db_suffix="_load_recovery")

    print(f"=== 4. leader re-election latency ({election_trials} trials) ===")
    election_latencies = await measure_election_latency(election_trials)
    results["election_latencies"] = election_latencies
    if election_latencies:
        print(f"  min/mean/max: {min(election_latencies):.3f} / "
              f"{sum(election_latencies) / len(election_latencies):.3f} / {max(election_latencies):.3f}")

    print("=== 5. throughput at N=1,2,3,4 ===")
    throughput = {}
    for n in (1, 2, 3, 4):
        t = await measure_throughput(n, throughput_uploads)
        throughput[str(n)] = t
        print(f"  N={n}: {t:.1f} uploads/sec" if t else f"  N={n}: FAILED")
    results["throughput"] = throughput

    with open(os.path.join(RESULTS_DIR, "results.json"), "w") as f:
        json.dump(results, f, indent=2)

    import csv
    with open(os.path.join(RESULTS_DIR, "summary.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["metric", "value"])
        w.writerow(["photo_upload_p50", results["photo_latency_percentiles"]["p50"]])
        w.writerow(["photo_upload_p95", results["photo_latency_percentiles"]["p95"]])
        w.writerow(["photo_upload_p99", results["photo_latency_percentiles"]["p99"]])
        w.writerow(["like_p50", results["like_latency_percentiles"]["p50"]])
        w.writerow(["like_p95", results["like_latency_percentiles"]["p95"]])
        w.writerow(["like_p99", results["like_latency_percentiles"]["p99"]])
        w.writerow(["convergence_seconds", results["convergence_seconds"]])
        w.writerow(["recovery_seconds", results["recovery_seconds"]])
        w.writerow(["gossip_cold_bytes", results["gossip_bandwidth"]["cold_bytes"]])
        w.writerow(["gossip_warm_bytes", results["gossip_bandwidth"]["warm_bytes"]])
        if election_latencies:
            w.writerow(["election_min_s", min(election_latencies)])
            w.writerow(["election_mean_s", sum(election_latencies) / len(election_latencies)])
            w.writerow(["election_max_s", max(election_latencies)])
        for n, t in throughput.items():
            w.writerow([f"throughput_n{n}_uploads_per_sec", t])

    save_graphs(results)
    print(f"\nResults written to {RESULTS_DIR}/ (results.json, summary.csv, *.png)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
