const GUEST_ID_KEY = "swarmlens_guest_id";

const ADJECTIVES = ["swift", "golden", "quiet", "bold", "lucky", "bright", "calm", "eager"];
const ANIMALS = ["otter", "falcon", "fox", "heron", "lynx", "wren", "hare", "mole"];

function randomGuestName(): string {
  const a = ADJECTIVES[Math.floor(Math.random() * ADJECTIVES.length)];
  const n = ANIMALS[Math.floor(Math.random() * ANIMALS.length)];
  return `${a}-${n}-${Math.floor(Math.random() * 100)}`;
}

/** A persistent per-browser-profile guest identity. There's no login
 * anywhere in this system -- guest_id is just a stable label a device
 * picks for itself once, same spirit as the vector clock's device_id. */
export function currentGuestId(): string {
  let id = localStorage.getItem(GUEST_ID_KEY);
  if (!id) {
    id = randomGuestName();
    localStorage.setItem(GUEST_ID_KEY, id);
  }
  return id;
}
