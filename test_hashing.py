"""Consistent hashing test: confirms the ring assigns zones deterministically
and that adding a member remaps roughly 1/N of them, not all of them. Pure
unit test -- hashing.py does no I/O, so this needs no running nodes."""

from hashing import ring_for, owner_of

ZONES = [f"zone_{i}" for i in range(200)]


def mapping_for(members):
    ring = ring_for(members)
    return {z: owner_of(z, ring) for z in ZONES}


def main():
    three = ["node1", "node2", "node3"]
    four = ["node1", "node2", "node3", "node4"]

    m3 = mapping_for(three)
    m4 = mapping_for(four)

    counts3 = {m: 0 for m in three}
    for owner in m3.values():
        counts3[owner] += 1
    print("3-node distribution:", counts3)

    moved = sum(1 for z in ZONES if m3[z] != m4[z])
    pct = moved / len(ZONES) * 100
    print(f"zones remapped after adding node4: {moved}/{len(ZONES)} ({pct:.1f}%)")
    print(f"expected roughly 1/{len(four)} of zones ({len(ZONES) // len(four)}) "
          f"if virtual nodes are spreading load evenly")

    assert moved < len(ZONES) * 0.5, "far more than 1/N remapped -- ring isn't working"

    # determinism: same members, same zones -> same mapping every time
    assert mapping_for(three) == m3, "ring is not deterministic"

    print("OK")


if __name__ == "__main__":
    main()
