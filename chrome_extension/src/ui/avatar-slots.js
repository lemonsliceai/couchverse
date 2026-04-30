/**
 * Avatar slot DOM — everything that touches `.avatar-slot` elements.
 *
 * Each slot represents one persona's panel: still preview, live video
 * mount point, badge, reactions overlay. The HTML declares the slots
 * up front (one per persona); this module fills them with runtime
 * content.
 */

const $ = (sel) => document.querySelector(sel);

export function slotFor(personaName) {
  if (!personaName) return null;
  return document.querySelector(`.avatar-slot[data-name="${personaName}"]`);
}

// Tracks which LiveKit room currently owns each slot's mounted video.
// Under the dual-room architecture two RoomControllers feed this same
// registry, but the architecture guarantees one persona ↔ one room — so
// the same slot should never be mounted from two different rooms. If
// that invariant ever breaks, `mountAvatarVideo` warns loudly with both
// room names rather than silently overwriting.
const slotRoomOwners = new WeakMap();

// Mount a freshly-attached LiveKit video element inside the slot's
// .avatar-video container. Sizing/object-fit live in CSS — this stays
// purely behavioral. `roomName` records which room the track came from
// so a second room mounting into the same slot is detectable.
export function mountAvatarVideo(slot, track, roomName = null) {
  const container = slot.querySelector(".avatar-video");
  if (!container) return;
  const existingRoom = slotRoomOwners.get(slot);
  if (roomName && existingRoom && existingRoom !== roomName) {
    console.warn(
      "[ext] avatar slot already owned by a different room — unexpected, expected one persona per room",
      "persona=", slot.dataset.name,
      "existingRoom=", existingRoom,
      "incomingRoom=", roomName,
    );
  }
  const el = track.attach();
  container.replaceChildren(el);
  if (roomName) slotRoomOwners.set(slot, roomName);
  // Swap the still preview for the live video. The `video-live` class
  // drives a fade-in on the video + fade-out on the still image so the
  // transition reads as the preview "animating into" the avatar.
  slot.classList.add("video-live", "breathing");
}

// Reset every slot to its preview-only state for the next session.
export function resetAllSlots() {
  for (const slot of document.querySelectorAll(".avatar-slot")) {
    slot.classList.remove("speaking", "breathing", "video-live");
    const videoContainer = slot.querySelector(".avatar-video");
    if (videoContainer) videoContainer.replaceChildren();
    slotRoomOwners.delete(slot);
  }
}

// Add or remove the "speaking" class for a given persona. Used by both
// commentary_start/end (authoritative) and active-speaker VAD updates
// (visual-only fallback).
export function setSlotSpeaking(personaName, isSpeaking) {
  const slot = slotFor(personaName);
  if (!slot) return;
  slot.classList.toggle("speaking", isSpeaking);
}

// ── Floating reactions ──

const REACTION_SETS = {
  laugh: ["\u{1F602}", "\u{1F923}", "\u{1F606}", "\u{1F60F}"],
  love: ["\u{2764}\u{FE0F}", "\u{1F9E1}", "\u{1F525}"],
  eyes: ["\u{1F440}", "\u{2728}", "\u{1F98A}"],
  fire: ["\u{1F525}", "\u{1F4A5}", "\u{26A1}"],
};
const REACTION_LIFETIME_MS = 2000;

export function spawnReaction(slot, type) {
  if (!slot) return;
  const container = slot.querySelector(".reactions");
  if (!container) return;

  const sets = Object.keys(REACTION_SETS);
  const key = type === "random" ? sets[Math.floor(Math.random() * sets.length)] : type;
  const emojis = REACTION_SETS[key] || REACTION_SETS.laugh;
  const emoji = emojis[Math.floor(Math.random() * emojis.length)];

  const particle = document.createElement("span");
  particle.className = "reaction-particle";
  particle.textContent = emoji;
  // Random horizontal drift gives the cluster shape rather than a vertical column.
  const drift = (Math.random() - 0.5) * 40;
  particle.style.setProperty("--drift", `${drift}px`);
  particle.style.animationDelay = `${Math.random() * 0.2}s`;

  container.appendChild(particle);
  setTimeout(() => particle.remove(), REACTION_LIFETIME_MS);
}

// ── Setup-screen status ──

export function showError(msg) {
  const el = $("#setup-error");
  if (!el) return;
  el.textContent = msg;
  el.classList.remove("hidden");
}

export function hideError() {
  $("#setup-error")?.classList.add("hidden");
}
