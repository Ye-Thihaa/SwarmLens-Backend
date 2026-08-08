# SwarmLens — three-node gossip backend

Three identical FastAPI nodes, each owning its own SQLite file. No shared
database. State converges by gossip, not by a central authority.

## Run

    docker compose up --build

Nodes land on http://localhost:8001, :8002, :8003

Local (no Docker), three terminals:

    NODE_ID=node1 DB_PATH=./node1.db SELF_URL=http://127.0.0.1:8001 PEERS=http://127.0.0.1:8002,http://127.0.0.1:8003 uvicorn main:app --port 8001
    NODE_ID=node2 DB_PATH=./node2.db SELF_URL=http://127.0.0.1:8002 PEERS=http://127.0.0.1:8001,http://127.0.0.1:8003 uvicorn main:app --port 8002
    NODE_ID=node3 DB_PATH=./node3.db SELF_URL=http://127.0.0.1:8003 PEERS=http://127.0.0.1:8001,http://127.0.0.1:8002 uvicorn main:app --port 8003

`SELF_URL` must be the address the *other* nodes reach this one at (i.e.
it appears in their `PEERS` list) — it's how every node's own identity on
the consistent-hashing ring matches how peers already refer to it.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/photos` | upload (guest_id, zone, composition_score) |
| GET  | `/photos` | gallery as this node currently sees it |
| POST | `/likes`  | like a photo (guest_id, photo_id) |
| GET  | `/zones`  | emergent aesthetic map, ranked; each zone's score comes from its owning node on the hash ring (`stale: true` if that node is unreachable) |
| GET  | `/zones/local` | this node's own replica of every zone, no ownership filtering — what peers proxy to |
| GET  | `/zones/ring` | debug: current ring membership and the zone → owner mapping |
| GET  | `/zones/quorum?R=&W=` | quorum read: union raw events from R of N random nodes and recompute; W is echoed back for the CAP narrative only |
| GET  | `/zones/events` | this node's raw photo/like events, undigested — what quorum reads fetch from each sampled peer |
| GET  | `/health` | event count, version vector, gossip stats |
| POST | `/gossip/sync` | peer-to-peer: exchange digest, return missing events |
| POST | `/gossip/push` | peer-to-peer: deliver events the peer lacks |
| POST | `/chaos/partition/{i}` | stop gossiping with PEERS[i] |
| POST | `/chaos/heal` | resume all gossip |
| POST | `/raft/request_vote` | peer-to-peer: cast a vote for a candidate |
| POST | `/raft/heartbeat` | peer-to-peer: leader keepalive |
| GET  | `/raft/status` | this node's Raft role, term, current leader |
| POST | `/recap/trigger` | fire the one-time recap; no-ops unless this node is leader |

## Verified demo

    # write to node1, read from node3 a second later
    curl -X POST localhost:8001/photos -H 'content-type: application/json' \
      -d '{"guest_id":"g1","zone":"flower_arch","composition_score":93}'
    curl localhost:8003/photos

    # partition, like from both sides, watch counts diverge
    curl -X POST localhost:8003/chaos/partition/0
    curl -X POST localhost:8003/chaos/partition/1
    curl -X POST localhost:8001/likes -H 'content-type: application/json' \
      -d '{"guest_id":"g2","photo_id":"ph_xxx"}'
    curl -X POST localhost:8003/likes -H 'content-type: application/json' \
      -d '{"guest_id":"g5","photo_id":"ph_xxx"}'
    curl localhost:8001/photos ; curl localhost:8003/photos   # different

    # heal, watch them converge with nothing lost
    curl -X POST localhost:8001/chaos/heal
    curl -X POST localhost:8003/chaos/heal

Observed run: node1=3, node3=2 while split; all three = 5 after healing.
A guest who liked twice was counted once.

## Raft leader election

Election-only Raft in `raft.py` (no log replication -- the event log +
gossip already handle that). Run `python test_raft.py` for an automated
demo: starts three nodes, confirms a single leader, `kill -9`s it,
confirms re-election within ~1s, and confirms `recap_sent` fires exactly
once cluster-wide even across the crash.

## Consistent hashing (zone ownership)

Each venue zone is owned by exactly one node, picked from a consistent
hash ring (`hashing.py`, 100 virtual nodes per member) over the currently
alive cluster (self + `gossip.alive_peers()`). `GET /zones` proxies each
zone to its owner; if the owner doesn't answer it falls back to this
node's own replica with `stale: true` rather than failing. `python
test_hashing.py` is a pure unit test confirming the ring's remap
property: adding a 4th node to a 3-node ring remaps roughly 1/4 of zones,
not all of them.

## Quorum reads (the CAP knob)

`GET /zones/quorum?R=2&W=2` samples `R` of `N` nodes at random each call
(`N = len(PEERS) + 1`, fixed), unions their raw photo/like events deduped
on `(origin, seq)`, and recomputes zone scores from the merge -- more
honest than trusting one node's already-aggregated counts. `W` is
accepted and echoed back for the CAP-tradeoff narrative only; this
system's writes are always local + eventually gossiped, so there's no
synchronous write-quorum for `W` to gate.

Demo the tradeoff live:

    # fully isolate node3 (both directions, from both other nodes)
    curl -X POST localhost:8001/chaos/partition/1
    curl -X POST localhost:8002/chaos/partition/1
    curl -X POST localhost:8003/chaos/partition/0
    curl -X POST localhost:8003/chaos/partition/1

    # write after the partition -- reaches node1 + node2, never node3
    curl -X POST localhost:8001/photos -H 'content-type: application/json' \
      -d '{"guest_id":"g1","zone":"new_zone","composition_score":93}'

    curl 'localhost:8001/zones/quorum?R=1'   # sometimes lands on node3, undercounts
    curl 'localhost:8001/zones/quorum?R=2'   # always fresh -- majority excludes node3

    curl -X POST localhost:8001/chaos/heal
    curl -X POST localhost:8002/chaos/heal
    curl -X POST localhost:8003/chaos/heal   # R=1 becomes reliably fresh again

`python test_quorum.py` runs this exact scenario automatically: R=1 over
30 tries came back fresh 21 and stale 9 times, R=2 was fresh 20/20, and
R=1 was fresh 10/10 again after healing.

## Next

- guest client: offline queue + vector clocks (Phase 5)
