import hashlib

VIRTUAL_NODES = 100


def ring_for(members: list[str]) -> list[tuple[int, str]]:
    points = []
    for m in members:
        for v in range(VIRTUAL_NODES):
            h = int(hashlib.md5(f"{m}#{v}".encode()).hexdigest(), 16)
            points.append((h, m))
    return sorted(points)


def owner_of(key: str, ring: list[tuple[int, str]]) -> str:
    h = int(hashlib.md5(key.encode()).hexdigest(), 16)
    for point_hash, member in ring:
        if h <= point_hash:
            return member
    return ring[0][1]   # wrap around
