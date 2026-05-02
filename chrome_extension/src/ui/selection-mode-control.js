/**
 * Selection-mode control — segmented control for the agent's
 * speaker-pick policy (Ordered / Shuffle / Director). Persisted to
 * chrome.storage.sync so the user's preference rides along with the
 * Chrome profile across devices.
 *
 * Mirrors pacing-controls.js's shape but storage is async, so the UI
 * paints the default first and re-syncs once the stored value loads.
 * The orchestration layer subscribes via `onChange` and is responsible
 * for publishing to the agent — this module doesn't know about LiveKit.
 */

import {
  SELECTION_MODE_DEFAULT,
  SELECTION_MODE_SCHEMA_VERSION,
  SELECTION_MODE_STORAGE_KEY,
  SELECTION_MODES,
} from "../config.js";

let mode = SELECTION_MODE_DEFAULT;
let onChangeCallback = null;

export async function initSelectionModeControl(onChange) {
  onChangeCallback = onChange ?? null;
  const group = groupEl();
  if (group) {
    syncGroup(group, mode);
    group.addEventListener("click", (ev) => {
      const btn = ev.target.closest(".seg-btn");
      if (!btn || !group.contains(btn)) return;
      selectMode(btn.dataset.value);
    });
  }
  // Storage load is async — the UI is already mounted at default; once
  // the persisted value comes back, re-sync (and notify only if it
  // differs from the default, so we don't redundantly publish "ordered"
  // every panel open).
  const stored = await loadMode();
  if (stored !== mode) {
    mode = stored;
    if (group) syncGroup(group, mode);
    onChangeCallback?.(mode);
  }
}

export function getSelectionMode() {
  return mode;
}

function selectMode(value) {
  if (!value || !SELECTION_MODES.includes(value) || mode === value) return;
  mode = value;
  saveMode();
  syncGroup(groupEl(), mode);
  onChangeCallback?.(mode);
}

function groupEl() {
  return document.querySelector('.segmented[data-setting="selection_mode"]');
}

function syncGroup(group, activeValue) {
  if (!group) return;
  for (const btn of group.querySelectorAll(".seg-btn")) {
    btn.classList.toggle("is-active", btn.dataset.value === activeValue);
  }
}

async function loadMode() {
  // chrome.storage.sync is unavailable in some test/runtime contexts;
  // fall through to the default rather than throwing.
  if (!chrome?.storage?.sync) return SELECTION_MODE_DEFAULT;
  try {
    const data = await chrome.storage.sync.get(SELECTION_MODE_STORAGE_KEY);
    const entry = data?.[SELECTION_MODE_STORAGE_KEY];
    if (entry?.version !== SELECTION_MODE_SCHEMA_VERSION) return SELECTION_MODE_DEFAULT;
    return SELECTION_MODES.includes(entry.mode) ? entry.mode : SELECTION_MODE_DEFAULT;
  } catch {
    return SELECTION_MODE_DEFAULT;
  }
}

function saveMode() {
  if (!chrome?.storage?.sync) return;
  chrome.storage.sync
    .set({
      [SELECTION_MODE_STORAGE_KEY]: { version: SELECTION_MODE_SCHEMA_VERSION, mode },
    })
    .catch((err) => console.warn("[ext] selection-mode persist failed:", err));
}
