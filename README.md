# SwarmLens — three-node gossip backend

Three identical FastAPI nodes, each owning its own SQLite file. No shared
database. State converges by gossip, not by a central authority.

## Run

    docker compose up --build

Nodes land on http://localhost:8001, :8002, :8003

Local (no Docker), three terminals:

    NODE_ID=node1 DB_PATH=./node1.db PEERS=http://127.0.0.1:8002,http://127.0.0.1:8003 uvicorn main:app --port 8001
    NODE_ID=node2 DB_PATH=./node2.db PEERS=http://127.0.0.1:8001,http://127.0.0.1:8003 uvicorn main:app --port 8002
    NODE_ID=node3 DB_PATH=./node3.db PEERS=http://127.0.0.1:8001,http://127.0.0.1:8002 uvicorn main:app --port 8003

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/photos` | upload (guest_id, zone, composition_score) |
| GET  | `/photos` | gallery as this node currently sees it |
| POST | `/likes`  | like a photo (guest_id, photo_id) |
| GET  | `/zones`  | emergent aesthetic map, ranked |
| GET  | `/health` | event count, version vector, gossip stats |
| POST | `/gossip/sync` | peer-to-peer: exchange digest, return missing events |
| POST | `/gossip/push` | peer-to-peer: deliver events the peer lacks |
| POST | `/chaos/partition/{i}` | stop gossiping with PEERS[i] |
| POST | `/chaos/heal` | resume all gossip |

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

## Next

- lease-based job dispatch for the AI worker pool
- Raft leader election (only the leader sends the recap)
- consistent hashing so each node owns a subset of zones
- quorum reads (W=2, R=2 of N=3) on /zones
