/**
 * DOM rendering for the persona lineup.
 *
 * The server's persona manifest (GET /api/personas, also embedded in
 * the POST /api/sessions response) is the single source of truth for
 * which personas the extension shows, in what order, with which label
 * and preview image. This module turns that manifest into the static
 * markup that used to live in sidepanel.html — keeping persona names
 * out of the HTML entirely.
 *
 * Convention: previews ship inside the extension at `icons/<file>`.
 * The server names the filename (`preview_filename`); the client always
 * resolves it under `icons/`. If the bundle is missing the asset, the
 * <img> onerror swap below falls back to the generic icon so the slot
 * still has a frame instead of a broken-image glyph.
 */

const FALLBACK_PREVIEW = "icons/icon128.png";

function resolvePreview(personaName, filename) {
  return `icons/${filename || `${personaName}_2x3.png`}`;
}

function badgeText({ label, descriptor }) {
  return descriptor ? `${label} - ${descriptor}` : label;
}

// Setup-screen cast portraits. Rendered before any session exists, so
// the user sees who they're about to share the couch with.
export function renderSetupSlots(personas) {
  const stack = document.getElementById("cast-stack");
  if (!stack) return;
  stack.replaceChildren();
  for (const p of personas) {
    const portrait = document.createElement("div");
    portrait.className = "cast-portrait";

    const img = document.createElement("img");
    img.src = resolvePreview(p.name, p.preview_filename);
    img.alt = p.label;
    img.onerror = () => {
      img.onerror = null;
      img.src = FALLBACK_PREVIEW;
    };

    const badgeWrap = document.createElement("div");
    badgeWrap.className = "cast-badge-wrap";
    const span = document.createElement("span");
    span.textContent = badgeText(p);
    badgeWrap.appendChild(span);

    portrait.append(img, badgeWrap);
    stack.appendChild(portrait);
  }
}

// Session-screen avatar slots. Built fresh from the per-session
// `personas[]` returned by POST /api/sessions so the live stack is
// guaranteed to match what the server actually minted.
export function renderAvatarSlots(personas) {
  const stack = document.getElementById("avatars-stack");
  if (!stack) return;
  stack.replaceChildren();
  for (const p of personas) {
    const slot = document.createElement("div");
    slot.className = "avatar-slot has-preview";
    slot.dataset.name = p.name;
    slot.dataset.label = badgeText(p);

    const img = document.createElement("img");
    img.className = "avatar-preview";
    img.src = resolvePreview(p.name, p.preview_filename);
    img.alt = p.label;
    img.onerror = () => {
      img.onerror = null;
      img.src = FALLBACK_PREVIEW;
    };

    const video = document.createElement("div");
    video.className = "avatar-video";

    const badge = document.createElement("div");
    badge.className = "avatar-badge";
    const badgeSpan = document.createElement("span");
    badgeSpan.textContent = badgeText(p);
    badge.appendChild(badgeSpan);

    const tuning = document.createElement("div");
    tuning.className = "tuning-chip";
    tuning.setAttribute("aria-live", "polite");
    tuning.innerHTML =
      '<span>Tuning in</span><span class="tuning-dots">' +
      "<span>.</span><span>.</span><span>.</span></span>";

    const reactions = document.createElement("div");
    reactions.className = "reactions";

    slot.append(img, video, badge, tuning, reactions);
    stack.appendChild(slot);
  }
}
