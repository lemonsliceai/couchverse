/**
 * Side panel — LiveKit connection, tab audio capture, avatar rendering, controls.
 *
 * This is the main entry point for the Chrome extension's UI logic. It's
 * bundled by esbuild into dist/sidepanel.js (which sidepanel.html loads).
 *
 * Audio flow:
 *   Tab audio (any site) → chrome.tabCapture → MediaStream → LiveKit track
 *   → Agent subscribes → Groq STT → Commentary generation
 */

import {
  Room,
  RoomEvent,
  Track,
  ConnectionState,
} from "livekit-client";

// ── DOM helpers ──
const $ = (sel) => document.querySelector(sel);

// ── API URL ──
// Unpacked/dev installs hit localhost so anyone cloning this repo can run
// the whole stack locally without editing code. Chrome Web Store installs
// hit the hosted production API. If you fork this project and publish your
// own build to the Web Store, change PROD_API_URL to point at your deployed
// API.
const LOCAL_API_URL = "http://localhost:8080";
const PROD_API_URL = "https://watch-with-fox.fly.dev";

function getApiUrl() {
  // `update_url` is injected into the manifest automatically for extensions
  // installed from the Chrome Web Store. It's absent for unpacked/dev loads.
  const isStoreInstall = "update_url" in chrome.runtime.getManifest();
  return isStoreInstall ? PROD_API_URL : LOCAL_API_URL;
}

// ── State ──
let room = null;
let activeTabId = null;
let tabAudioStream = null;
let tabAudioContext = null;
let tabAudioGain = null;
let ducking = false;
// Guards against a rapid End → Start double-click re-entering the flow
// while the room is still tearing down. `endSession` holds this across
// the full `room.disconnect()` promise; `startSession` refuses to run
// until it clears. Without this, a new session can publish its
// podcast-audio track before the old room's disconnect has reached the
// server, and the agent briefly sees two user participants.
let sessionBusy = false;
// Tracks which personas are currently mid-utterance. Ducking holds while
// the set is non-empty so back-to-back turns from different speakers don't
// punch the video back up between them.
const speakingNow = new Set();
// Per-persona caption history keyed by persona name (e.g. "fox", "chaos_agent").
const captionsByPersona = new Map();
// Currently-connected LemonSlice avatar personas. When this drains to empty
// after at least one has connected, the session auto-ends — there's no point
// staying on the session screen with no comedians left to chime in.
const connectedAvatars = new Set();
let everHadAvatar = false;

// LemonSlice avatar participants are named lemonslice-avatar-<persona>.
// Routing decisions in onTrackSubscribed / onActiveSpeakers parse the suffix.
const AVATAR_IDENTITY_PREFIX = "lemonslice-avatar-";

function personaFromAvatarIdentity(identity) {
  if (!identity || !identity.startsWith(AVATAR_IDENTITY_PREFIX)) return null;
  return identity.slice(AVATAR_IDENTITY_PREFIX.length);
}

function slotFor(personaName) {
  if (!personaName) return null;
  return document.querySelector(`.avatar-slot[data-name="${personaName}"]`);
}

// ── Init ──
document.addEventListener("DOMContentLoaded", async () => {
  // Detect active media tab
  detectActiveMedia();

  // Wire up controls
  $("#start-btn").addEventListener("click", startSession);
  $("#end-btn").addEventListener("click", endSession);
  $("#skip-btn").addEventListener("click", skipCommentary);
  initPacingControls();

  // Listen for content script messages relayed through background
  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type === "media-state-update") {
      handleMediaStateUpdate(msg);
    }
    if (msg.type === "media-video-info") {
      updateVideoPreview(msg);
    }
  });
});

// ── Active Tab / Media Detection ──
// Detection never blocks on the content script — if the page was open before
// the extension was (re)loaded, the content script was never injected into
// it and `chrome.tabs.sendMessage` would fail silently, leaving the UI stuck
// on "Detecting video...". Instead, derive what we can (URL, title) directly
// from the tab's own metadata, which is always available via `activeTab`.
//
// The content script is still useful for runtime events (play/pause/seek
// monitoring during a session), so if it isn't responding we inject it
// programmatically via chrome.scripting.
async function detectActiveMedia() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  const tab = tabs[0];
  if (!tab) return;

  activeTabId = tab.id;

  if (!isCapturableTabUrl(tab.url)) {
    showNoVideoState();
    return;
  }

  // Use the tab's own metadata as the immediate preview. Strip common
  // " - Site Name" suffixes for nicer display.
  const title = stripTitleSuffix(tab.title || "") || tab.url;
  updateVideoPreview({ url: tab.url, title });

  // Ping the content script for richer info (and to confirm it's alive).
  // If it doesn't reply, inject it so play/pause monitoring works once the
  // session starts.
  try {
    const info = await chrome.tabs.sendMessage(tab.id, { type: "get-video-info" });
    if (info) updateVideoPreview(info);
  } catch {
    console.log("[ext] Content script not present, injecting...");
    try {
      await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        files: ["content.js"],
      });
      // Freshly-injected script will push a media-video-info message shortly.
    } catch (err) {
      console.warn("[ext] Content script injection failed:", err);
    }
  }
}

function isCapturableTabUrl(url) {
  if (!url) return false;
  // chrome://, edge://, about:, file:, view-source: etc. can't be tab-captured.
  return url.startsWith("http://") || url.startsWith("https://");
}

// Trim the trailing " - Site Name" / " | Site Name" / " — Site Name" that
// most sites tack onto <title>. Leaves the leading content (which is almost
// always the actual media title) untouched.
function stripTitleSuffix(title) {
  return title
    .replace(/\s+[-|–—]\s+[^-|–—]+$/, "")
    .trim();
}

function showNoVideoState() {
  $("#start-btn").disabled = true;
  delete $("#start-btn").dataset.videoUrl;
  delete $("#start-btn").dataset.videoTitle;
}

function updateVideoPreview(info) {
  if (info.url) {
    $("#start-btn").disabled = false;
    $("#start-btn").dataset.videoUrl = info.url;
    $("#start-btn").dataset.videoTitle = info.title || "";
  }
}

// ── Session Lifecycle ──
async function startSession() {
  const btn = $("#start-btn");
  // Prevent re-entering mid-teardown: `endSession` holds `sessionBusy`
  // across the room.disconnect promise, so a stray click here during that
  // window would otherwise start a new room before the old one is gone.
  if (sessionBusy) return;
  const videoUrl = btn.dataset.videoUrl;
  const videoTitle = btn.dataset.videoTitle || "";
  const apiUrl = getApiUrl();

  if (!videoUrl) {
    showError("No active media tab detected");
    return;
  }

  sessionBusy = true;
  btn.disabled = true;
  btn.classList.add("loading");
  btn.textContent = "Starting...";
  hideError();

  try {
    // 1. Create session via API
    const session = await createSessionApi(apiUrl, videoUrl, videoTitle);

    // 2. Show session screen
    $("#setup-screen").classList.add("hidden");
    $("#session-screen").classList.remove("hidden");

    // 3. Connect to LiveKit
    await connectRoom(session.token, session.livekit_url);

    // 4. Capture and publish tab audio
    await captureAndPublishTabAudio();

    console.log("[ext] Session started:", session.session_id);
  } catch (err) {
    console.error("[ext] Failed to start session:", err);
    showError(err.message);
    btn.disabled = false;
    btn.classList.remove("loading");
    btn.textContent = "Watch with Fox & Alien";
    $("#setup-screen").classList.remove("hidden");
    $("#session-screen").classList.add("hidden");
  } finally {
    sessionBusy = false;
  }
}

async function endSession() {
  if (sessionBusy) return;
  sessionBusy = true;

  const endBtn = $("#end-btn");
  if (endBtn) endBtn.disabled = true;

  try {
    if (room) {
      const prior = room;
      room = null;
      // `disconnect(true)` returns a promise that resolves once the
      // LiveKit transport is actually closed. Awaiting it prevents a
      // new Start from racing with the old room's teardown.
      try {
        await prior.disconnect(true);
      } catch (err) {
        console.warn("[ext] room.disconnect raised:", err);
      }
    }
    teardownTabAudio();
    if (unduckTimer) {
      clearTimeout(unduckTimer);
      unduckTimer = null;
    }
    ducking = false;
    speakingNow.clear();
    updateSkipButton();
    connectedAvatars.clear();
    everHadAvatar = false;
    captionsByPersona.clear();
    document
      .querySelectorAll(".avatar-slot .captions")
      .forEach((el) => (el.innerHTML = ""));
    document
      .querySelectorAll(".avatar-slot")
      .forEach((el) => {
        el.classList.remove("speaking", "breathing", "video-live");
        // Drop the live video element so the next session starts from a
        // clean preview-only state.
        const videoContainer = el.querySelector(".avatar-video");
        if (videoContainer) videoContainer.innerHTML = "";
      });

    // Return to setup screen
    $("#session-screen").classList.add("hidden");
    $("#setup-screen").classList.remove("hidden");
    const btn = $("#start-btn");
    btn.disabled = false;
    btn.classList.remove("loading");
    btn.textContent = "Watch with Fox";

    // Re-detect media in the active tab
    detectActiveMedia();
  } finally {
    if (endBtn) endBtn.disabled = false;
    sessionBusy = false;
  }
}

// ── API ──
async function createSessionApi(apiUrl, videoUrl, videoTitle) {
  const res = await fetch(`${apiUrl}/api/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      video_url: videoUrl,
      video_title: videoTitle,
    }),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`Session creation failed: ${text}`);
  }
  return res.json();
}

// ── LiveKit Room ──
async function connectRoom(token, livekitUrl) {
  room = new Room({
    adaptiveStream: true,
    dynacast: true,
  });

  room.on(RoomEvent.TrackSubscribed, onTrackSubscribed);
  room.on(RoomEvent.TrackUnsubscribed, onTrackUnsubscribed);
  room.on(RoomEvent.DataReceived, onDataReceived);
  room.on(RoomEvent.ActiveSpeakersChanged, onActiveSpeakers);
  room.on(RoomEvent.ConnectionStateChanged, onConnectionState);
  room.on(RoomEvent.Disconnected, onDisconnected);
  room.on(RoomEvent.ParticipantConnected, onParticipantConnected);
  room.on(RoomEvent.ParticipantDisconnected, onParticipantDisconnected);

  await room.connect(livekitUrl, token);
  console.log("[ext] Connected to LiveKit room");
}

// ── Tab Audio Capture ──
async function captureAndPublishTabAudio() {
  // 1. Request stream ID from background service worker
  const response = await new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(
      { type: "capture-tab-audio", tabId: activeTabId },
      (resp) => {
        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message));
          return;
        }
        if (!resp || resp.error) {
          reject(new Error(resp?.error || "Failed to capture tab audio"));
          return;
        }
        resolve(resp);
      }
    );
  });

  // 2. Get MediaStream from the stream ID.
  //
  // Disable echoCancellation / noiseSuppression / autoGainControl. getUserMedia
  // turns these on by default, and AGC in particular quietly attenuates loud
  // tab audio to normalize loudness — perceived as a small volume drop the
  // moment capture starts. Turning them off keeps the loopback bit-perfect so
  // tab volume stays put, and the avatar voices (played through <audio> at
  // default 1.0 gain) sit at the same reference level as the untouched tab.
  tabAudioStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      mandatory: {
        chromeMediaSource: "tab",
        chromeMediaSourceId: response.streamId,
      },
      optional: [
        { echoCancellation: false },
        { noiseSuppression: false },
        { autoGainControl: false },
      ],
    },
  });

  const audioTracks = tabAudioStream.getAudioTracks();
  if (audioTracks.length === 0) {
    throw new Error("No audio tracks in tab capture stream");
  }

  // 3. Route the captured audio back to the user's speakers.
  //
  // chrome.tabCapture intercepts the tab's audio output — without this
  // loopback the page would appear to mute the moment we start capturing.
  // Piping through an AudioContext to `destination` plays the same audio
  // the agent receives back out through the local speakers. The gain node
  // is used solely for ducking while Fox is talking; at rest it stays at
  // 1.0 so the page's own volume is preserved.
  tabAudioContext = new AudioContext();
  // Side panels are usually activated by a user gesture, but some Chromium
  // builds still create the context in "suspended" state. Explicit resume
  // makes the loopback audible immediately.
  if (tabAudioContext.state === "suspended") {
    await tabAudioContext.resume();
  }
  const source = tabAudioContext.createMediaStreamSource(tabAudioStream);
  tabAudioGain = tabAudioContext.createGain();
  source.connect(tabAudioGain);
  tabAudioGain.connect(tabAudioContext.destination);

  // 4. Publish the tab audio track to LiveKit.
  //
  // `Source.ScreenShareAudio` is the semantically correct source for
  // captured tab/window audio. Using it (instead of Unknown) ensures
  // LiveKit auto-subscribe works reliably — the agent's room-level
  // track_subscribed handler then matches on `name === "podcast-audio"`
  // and attaches it to the STT pipeline.
  const publication = await room.localParticipant.publishTrack(audioTracks[0], {
    name: "podcast-audio",
    source: Track.Source.ScreenShareAudio,
  });

  console.log(
    "[ext] Published podcast-audio:",
    "sid=", publication?.trackSid,
    "kind=", publication?.kind,
    "source=", publication?.source,
    "muted=", audioTracks[0].muted,
    "readyState=", audioTracks[0].readyState,
  );

  // If the track goes muted / ends unexpectedly, surface it. This helps
  // diagnose cases where tabCapture succeeds but silently stops producing
  // audio (e.g. user switched tabs or the tab was closed).
  audioTracks[0].addEventListener("mute", () =>
    console.warn("[ext] podcast-audio track muted")
  );
  audioTracks[0].addEventListener("ended", () =>
    console.warn("[ext] podcast-audio track ended")
  );
}

function teardownTabAudio() {
  if (tabAudioContext) {
    try { tabAudioContext.close(); } catch {}
    tabAudioContext = null;
    tabAudioGain = null;
  }
  if (tabAudioStream) {
    tabAudioStream.getTracks().forEach((t) => t.stop());
    tabAudioStream = null;
  }
}

// ── LiveKit Event Handlers ──
function onTrackSubscribed(track, publication, participant) {
  const personaName = personaFromAvatarIdentity(participant.identity);
  const isAvatarTrack =
    personaName !== null || participant.attributes?.["lk.publish_on_behalf"];

  // Audio from non-avatar participants is the persona voice itself
  // (when published directly without LemonSlice). Pipe it to the audio
  // container so it's audible even if the avatar pipeline is down.
  if (!isAvatarTrack && track.kind === Track.Kind.Audio) {
    const el = track.attach();
    $("#audio-container").appendChild(el);
    return;
  }
  if (!isAvatarTrack) return;

  // For avatar tracks we now know which slot they belong to.
  const slot = slotFor(personaName);

  if (track.kind === Track.Kind.Video && slot) {
    const container = slot.querySelector(".avatar-video");
    const el = track.attach();
    el.style.width = "100%";
    el.style.height = "100%";
    el.style.objectFit = "cover";
    el.style.borderRadius = "15px";
    container.innerHTML = "";
    container.appendChild(el);
    // Swap the still preview for the live video. The `video-live` class
    // drives a fade-in on the video + fade-out on the still image so the
    // transition reads as the preview "animating into" the avatar.
    slot.classList.add("video-live", "breathing");
    spawnReaction(slot, "eyes");
    return;
  }

  if (track.kind === Track.Kind.Audio) {
    const el = track.attach();
    $("#audio-container").appendChild(el);
  }
}

function onTrackUnsubscribed(track) {
  track.detach().forEach((el) => el.remove());
}

function onDataReceived(payload, participant, kind, topic) {
  let msg;
  try {
    msg = JSON.parse(new TextDecoder().decode(payload));
  } catch {
    return;
  }

  // Agent ready handshake — sync current playhead + push the user's
  // saved pacing preferences so they take effect from the first turn.
  if (topic === "commentary.control" && msg.type === "agent_ready") {
    console.log("[ext] Agent ready — syncing playhead");
    syncPlayheadToAgent();
    publishPacing();
    return;
  }

  // Commentary lifecycle — authoritative source for ducking and per-slot
  // speaker highlighting. The Director tags every commentary_start/end
  // with the persona name so we know which slot lights up.
  if (topic === "commentary.control" && msg.type === "commentary_start") {
    const personaName = msg.speaker;
    if (personaName) {
      speakingNow.add(personaName);
      const slot = slotFor(personaName);
      slot?.classList.add("speaking");
      spawnReaction(slot, "random");
    }
    setDucking(true);
    updateSkipButton();
    return;
  }

  if (topic === "commentary.control" && msg.type === "commentary_end") {
    const personaName = msg.speaker;
    if (personaName) {
      speakingNow.delete(personaName);
      slotFor(personaName)?.classList.remove("speaking");
    }
    if (speakingNow.size === 0) {
      setDucking(false);
    }
    updateSkipButton();
    return;
  }

  // Captions
  if (msg.type === "agent_transcript" || msg.text) {
    const text = msg.text || msg.content;
    const personaName = msg.speaker || guessSpeakerFromState();
    if (text) addCaption(personaName, text);
  }
}

// Fallback when a transcript message doesn't carry a `speaker` field
// (older agent build). Pick the persona currently mid-utterance, or fall
// back to the first slot if nobody is.
function guessSpeakerFromState() {
  if (speakingNow.size === 1) return speakingNow.values().next().value;
  const first = document.querySelector(".avatar-slot");
  return first?.dataset.name || null;
}

// VAD-driven active-speaker updates highlight the matching slot only
// when commentary.control hasn't already lit it. Purely visual jitter is
// acceptable here; commentary_start/end remains the authoritative source.
function onActiveSpeakers(speakers) {
  const localId = room?.localParticipant?.identity;
  const activePersonas = new Set();
  for (const p of speakers) {
    if (p.identity === localId) continue;
    const personaName = personaFromAvatarIdentity(p.identity);
    if (personaName) activePersonas.add(personaName);
  }
  for (const slot of document.querySelectorAll(".avatar-slot")) {
    const name = slot.dataset.name;
    if (activePersonas.has(name) || speakingNow.has(name)) {
      slot.classList.add("speaking");
    } else {
      slot.classList.remove("speaking");
    }
  }
}

function onConnectionState(state) {
  console.log("[ext] Connection state:", state);
}

function onDisconnected(reason) {
  console.log("[ext] Disconnected:", reason);
}

function onParticipantConnected(participant) {
  const personaName = personaFromAvatarIdentity(participant.identity);
  if (!personaName) return;
  connectedAvatars.add(personaName);
  everHadAvatar = true;
}

// Once every avatar that joined this session has left, there's no commentary
// coming — auto-end so the user lands back on the start screen instead of an
// empty stage. The `everHadAvatar` gate prevents this from firing during the
// initial connect window before any avatar has shown up.
function onParticipantDisconnected(participant) {
  const personaName = personaFromAvatarIdentity(participant.identity);
  if (!personaName) return;
  connectedAvatars.delete(personaName);
  if (everHadAvatar && connectedAvatars.size === 0) {
    endSession();
  }
}

// ── Playhead Sync ──
async function syncPlayheadToAgent() {
  if (!room || !activeTabId) return;

  try {
    const state = await chrome.tabs.sendMessage(activeTabId, {
      type: "get-video-state",
    });
    if (!state) return;

    const SYNC_FORWARD_SEC = 0.7;
    if (state.playing) {
      await publishControl(
        { type: "play", t: Math.max(0, state.time + SYNC_FORWARD_SEC) },
        "podcast.control"
      );
    } else {
      await publishControl({ type: "pause" }, "podcast.control");
    }
  } catch (err) {
    console.warn("[ext] Failed to sync playhead:", err);
  }
}

// ── Skip Commentary ──
// Tells the agent to cut off whoever's mid-utterance. Button stays disabled
// until `commentary_start` flips speakingNow non-empty, so a click is always
// targeting an actual in-flight turn. The agent answers by interrupting each
// persona's SpeechHandle, which in turn fires `commentary_end` — the normal
// handler below clears slot highlights and un-ducks.
function skipCommentary() {
  if (speakingNow.size === 0) return;
  publishControl({ type: "skip" }, "podcast.control");
}

function updateSkipButton() {
  const btn = $("#skip-btn");
  if (!btn) return;
  btn.disabled = speakingNow.size === 0;
}

// ── Pacing controls (Chattiness / Reply length) ──
// Two segmented controls wired through a single handler. Choices persist
// across sessions in localStorage and are re-sent after `agent_ready` so a
// freshly-connected agent picks them up. Before the room connects, clicks
// still update the UI + localStorage — they take effect next session.
const PACING_STORAGE_KEY = "watch-with-fox.pacing";
const PACING_DEFAULTS = { frequency: "normal", length: "normal" };
const pacing = { ...PACING_DEFAULTS };

function initPacingControls() {
  Object.assign(pacing, loadPacing());
  for (const group of document.querySelectorAll(".segmented")) {
    const setting = group.dataset.setting;
    if (!setting) continue;
    syncSegmentedGroup(group, pacing[setting]);
    group.addEventListener("click", (ev) => {
      const btn = ev.target.closest(".seg-btn");
      if (!btn || !group.contains(btn)) return;
      selectPacing(setting, btn.dataset.value);
    });
  }
}

function selectPacing(setting, value) {
  if (!value || pacing[setting] === value) return;
  pacing[setting] = value;
  savePacing();
  const group = document.querySelector(`.segmented[data-setting="${setting}"]`);
  if (group) syncSegmentedGroup(group, value);
  publishPacing();
}

function syncSegmentedGroup(group, activeValue) {
  for (const btn of group.querySelectorAll(".seg-btn")) {
    btn.classList.toggle("is-active", btn.dataset.value === activeValue);
  }
}

function publishPacing() {
  publishControl(
    { type: "settings", frequency: pacing.frequency, length: pacing.length },
    "podcast.control",
  );
}

function loadPacing() {
  try {
    const raw = localStorage.getItem(PACING_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return {
      frequency: parsed.frequency || PACING_DEFAULTS.frequency,
      length: parsed.length || PACING_DEFAULTS.length,
    };
  } catch {
    return {};
  }
}

function savePacing() {
  try {
    localStorage.setItem(PACING_STORAGE_KEY, JSON.stringify(pacing));
  } catch {
    // Private mode / quota — silently ignore; the UI still works per-session.
  }
}

function handleMediaStateUpdate(msg) {
  if (!room || room.state !== ConnectionState.Connected) return;

  // Pausing the media cuts the tab-audio track the agent relies on for STT,
  // so there's nothing left for Fox to react to. End the session rather
  // than leave the avatar streaming into silence.
  if (!msg.playing) {
    endSession();
    return;
  }

  publishControl({ type: "play", t: msg.time }, "podcast.control");
}

// ── Data Channel ──
async function publishControl(payload, topic) {
  if (!room || room.state !== ConnectionState.Connected) return;
  try {
    const encoder = new TextEncoder();
    await room.localParticipant.publishData(
      encoder.encode(JSON.stringify(payload)),
      { reliable: true, topic }
    );
  } catch (err) {
    console.warn("[ext] publishData failed:", err);
  }
}

// ── Ducking ──
// When Fox speaks, drop the video to a low but still-audible level rather
// than muting. Around -12 dB (25%) is the standard range for dialog ducking;
// going much lower makes the transitions feel dramatic and pump-y. At rest
// the gain is 1.0 — we never modify the user's own page/system volume.
const DUCK_GAIN = 0.25;
const PASSTHROUGH_GAIN = 1.0;

// Release hold on un-duck. Prevents brief gaps (late commentary_end, dropped
// packets) from punching the video back up mid-utterance. 600ms is the
// conventional sweet spot for speech ducking.
const UNDUCK_RELEASE_MS = 600;

// Exponential-ramp time constants for the gain node (seconds). Fast attack
// so Fox isn't stepped on, slower release so the recovery is inaudible.
// setTargetAtTime uses these as the 63%-of-target time constant.
const DUCK_ATTACK_TAU = 0.05;
const DUCK_RELEASE_TAU = 0.25;

let unduckTimer = null;

// Single entry point for toggling the ducking state. On un-duck we hold for
// UNDUCK_RELEASE_MS before actually releasing, to ride over any short gaps.
function setDucking(active) {
  if (active) {
    if (unduckTimer) {
      clearTimeout(unduckTimer);
      unduckTimer = null;
    }
    if (!ducking) {
      ducking = true;
      applyDucking();
    }
    return;
  }
  if (unduckTimer) return;
  unduckTimer = setTimeout(() => {
    unduckTimer = null;
    ducking = false;
    applyDucking();
  }, UNDUCK_RELEASE_MS);
}

function applyDucking() {
  if (!tabAudioGain || !tabAudioContext) return;
  // Ramp the gain exponentially instead of snapping — an instantaneous
  // .value = x is audible as a click/pump; setTargetAtTime fades smoothly
  // with no zipper noise.
  const now = tabAudioContext.currentTime;
  const target = ducking ? DUCK_GAIN : PASSTHROUGH_GAIN;
  const tau = ducking ? DUCK_ATTACK_TAU : DUCK_RELEASE_TAU;
  tabAudioGain.gain.cancelScheduledValues(now);
  tabAudioGain.gain.setTargetAtTime(target, now, tau);
}

// ── Captions (Speech Bubbles) ──
function addCaption(personaName, text) {
  if (!personaName) return;
  const slot = slotFor(personaName);
  if (!slot) return;
  const list = captionsByPersona.get(personaName) || [];
  list.push(text);
  while (list.length > 3) list.shift();
  captionsByPersona.set(personaName, list);
  renderCaptions(slot, list);
}

function renderCaptions(slot, list) {
  const container = slot.querySelector(".captions");
  if (!container) return;
  container.innerHTML = list
    .map((c) => `<div class="speech-bubble">${escapeHtml(c)}</div>`)
    .join("");
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

// ── Floating Reactions ──
const REACTION_SETS = {
  laugh: ["\u{1F602}", "\u{1F923}", "\u{1F606}", "\u{1F60F}"],
  love:  ["\u{2764}\u{FE0F}", "\u{1F9E1}", "\u{1F525}"],
  eyes:  ["\u{1F440}", "\u{2728}", "\u{1F98A}"],
  fire:  ["\u{1F525}", "\u{1F4A5}", "\u{26A1}"],
};

function spawnReaction(slot, type) {
  if (!slot) return;
  const container = slot.querySelector(".reactions");
  if (!container) return;

  // Pick a random set if type is "random"
  const sets = Object.keys(REACTION_SETS);
  const key = type === "random" ? sets[Math.floor(Math.random() * sets.length)] : type;
  const emojis = REACTION_SETS[key] || REACTION_SETS.laugh;
  const emoji = emojis[Math.floor(Math.random() * emojis.length)];

  const particle = document.createElement("span");
  particle.className = "reaction-particle";
  particle.textContent = emoji;
  // Random horizontal drift
  const drift = (Math.random() - 0.5) * 40;
  particle.style.setProperty("--drift", `${drift}px`);
  particle.style.animationDelay = `${Math.random() * 0.2}s`;

  container.appendChild(particle);

  // Clean up after animation
  setTimeout(() => particle.remove(), 2000);
}

// ── UI Helpers ──
function showError(msg) {
  const el = $("#setup-error");
  el.textContent = msg;
  el.classList.remove("hidden");
}

function hideError() {
  $("#setup-error").classList.add("hidden");
}
