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
  return descriptor || label;
}

// Column count baked into --slot-min below: 1 col for N≤3, 2 for N=4,
// 3 for N=5-9, 4 for N≥10. Mirroring this here lets us derive the row
// count so CSS can size each tile to fit the container's height.
function colsFor(n) {
  if (n <= 3) return 1;
  if (n <= 4) return 2;
  if (n <= 9) return 3;
  return 4;
}

// Setup-screen cast portraits. Rendered before any session exists, so
// the user sees who they're about to share the couch with.
export function renderSetupSlots(personas) {
  const stack = document.getElementById("cast-stack");
  if (!stack) return;
  stack.replaceChildren();

  // Same N-aware grid sizing as renderAvatarSlots, with a wider gap
  // (14px) to match the existing setup-screen rhythm. 1-3 personas
  // stay as a single tall column; N=4 lands on 2 cols (2x2), 5-9 on 3
  // cols, 10+ on 4 cols. --rows lets the CSS sizing rule cap each
  // tile's height to its share of the container so the whole block
  // can sit centered without overflow.
  const n = personas.length;
  const cols = colsFor(n);
  const rows = Math.ceil(n / cols);
  let slotMin;
  if (n <= 3) slotMin = "100%";
  else if (n <= 4) slotMin = "calc(50% - 7px)";
  else if (n <= 9) slotMin = "calc(33.333% - 10px)";
  else slotMin = "calc(25% - 11px)";
  stack.style.setProperty("--slot-min", slotMin);
  stack.style.setProperty("--rows", String(rows));
  stack.dataset.n = n <= 3 ? "stack" : n <= 9 ? "grid" : "dense";

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

  // N-aware grid sizing. The CSS grid-template uses
  // `repeat(auto-fit, minmax(var(--slot-min), 1fr))`, so picking the
  // minmax floor here is what decides how many columns we get for the
  // current persona count. 1-3 personas stay as a single tall column
  // (full-width tiles read better than half-empty grid rows); the grid
  // kicks in at N=4 (2 cols), N=5-9 (3 cols), N=10+ (4 cols). --rows
  // feeds the CSS sizing rule so each tile gets capped to its share of
  // the container's height — the grid block then sits centered in
  // whatever vertical space is left over.
  const n = personas.length;
  const cols = colsFor(n);
  const rows = Math.ceil(n / cols);
  let slotMin;
  if (n <= 3) slotMin = "100%";
  else if (n <= 4) slotMin = "calc(50% - 5px)";
  else if (n <= 9) slotMin = "calc(33.333% - 7px)";
  else slotMin = "calc(25% - 8px)";
  stack.style.setProperty("--slot-min", slotMin);
  stack.style.setProperty("--rows", String(rows));
  stack.dataset.n = n <= 3 ? "stack" : n <= 9 ? "grid" : "dense";

  for (const p of personas) {
    const slot = document.createElement("div");
    slot.className = "avatar-slot has-preview";
    slot.dataset.name = p.name;
    slot.dataset.label = badgeText(p);

    // Per-persona accent — drives the .speaking outline + shadow via
    // CSS vars so adding a persona is config (manifest), not code.
    if (p.accent_color) slot.style.setProperty("--slot-accent", p.accent_color);
    if (p.accent_color_deep) slot.style.setProperty("--slot-accent-deep", p.accent_color_deep);

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
