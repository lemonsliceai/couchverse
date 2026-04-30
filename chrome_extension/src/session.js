/**
 * SessionLifecycle — orchestrates start/end of a session and routes
 * room events into the audio graph + UI. This is the only place where
 * the audio graph, room controller, tab capture, and UI all meet.
 *
 * State is owned via the `SessionState` enum (see config.js):
 *   IDLE → STARTING → LIVE → ENDING → IDLE
 *
 * Auto-end paths (avatars all gone, tab paused) check for `LIVE` so
 * they can't re-enter end during a teardown already in flight.
 */

import { DisconnectReason, Track } from "livekit-client";

import { AudioGraph } from "./audio/audio-graph.js";
import { SessionState } from "./config.js";
import { EventDeduper } from "./livekit/event-deduper.js";
import { RoomController } from "./livekit/room-controller.js";
import { detectActiveMedia, syncPlayheadToAgent } from "./messaging/tab-bridge.js";
import { personaFromAvatarIdentity, resolvePersonaKey } from "./persona.js";
import { createSessionApi, friendlyApiError } from "./transport/api.js";
import { captureAndPublishTabAudio, stopTabStream } from "./transport/tab-capture.js";
import {
  hideError,
  mountAvatarVideo,
  resetAllSlots,
  setSlotSpeaking,
  showError,
  slotFor,
  spawnReaction,
} from "./ui/avatar-slots.js";
import { getPacing } from "./ui/pacing-controls.js";

const $ = (sel) => document.querySelector(sel);

// Parse the POST /api/sessions response into the internal session shape.
// Validates the top-level fields and that exactly one room is marked
// `role: "primary"` — anything else means the agent and API are out of
// sync and we'd rather fail fast at start than wire up a half-broken
// room topology.
function parseSession(payload) {
  if (!payload || typeof payload.session_id !== "string") {
    throw new Error("Session response missing session_id");
  }
  if (typeof payload.livekit_url !== "string") {
    throw new Error("Session response missing livekit_url");
  }
  if (!Array.isArray(payload.rooms) || payload.rooms.length === 0) {
    throw new Error("Session response missing rooms[]");
  }

  const rooms = new Map();
  let primaryCount = 0;
  for (const entry of payload.rooms) {
    if (
      !entry ||
      typeof entry.persona !== "string" ||
      typeof entry.room_name !== "string" ||
      typeof entry.token !== "string" ||
      (entry.role !== "primary" && entry.role !== "secondary")
    ) {
      throw new Error(`Malformed room entry: ${JSON.stringify(entry)}`);
    }
    if (rooms.has(entry.persona)) {
      throw new Error(`Duplicate persona in rooms[]: ${entry.persona}`);
    }
    if (entry.role === "primary") primaryCount++;
    rooms.set(entry.persona, {
      roomName: entry.room_name,
      token: entry.token,
      role: entry.role,
    });
  }
  if (primaryCount !== 1) {
    throw new Error(`Expected exactly one primary room, got ${primaryCount}`);
  }

  return {
    sessionId: payload.session_id,
    livekitUrl: payload.livekit_url,
    rooms,
  };
}

export class SessionLifecycle {
  constructor() {
    this._state = SessionState.IDLE;
    this._activeTabId = null;
    this._tabAudioStream = null;
    // Per-session payload from POST /api/sessions. Holds
    // `{sessionId, livekitUrl, rooms: Map<persona, …>}`. Cleared on end
    // / partial-start failure.
    this._session = null;

    // Primary RoomController. Aliases `_controllers.get(_primaryPersona)`.
    // Outbound user commands (skip, pacing, play/pause sync) all publish
    // on this controller — never on a secondary — so the agent's
    // in-process fan-out is the single source of truth for cross-persona
    // ordering.
    this._room = null;
    // Map<persona, RoomController>, one entry per room in the API's
    // `rooms[]` response.
    this._controllers = null;
    // Persona key whose controller is the primary command channel.
    // Resolved at start time from the API's `role: "primary"` entry.
    this._primaryPersona = null;
    this._audio = new AudioGraph({ audioContainer: $("#audio-container") });

    // UI-only: which personas are currently mid-utterance. Drives slot
    // highlighting and Skip button state. Does NOT drive audio ducking
    // — that's signal-driven off the voice analysers, not these events,
    // so a late commentary_end from LemonSlice can't leave the tab
    // stuck ducked.
    this._speakingNow = new Set();
    // Personas currently mid-intro. Tracked separately so the Skip
    // button can stay disabled during intros — the intro ritual is
    // non-skippable.
    this._introNow = new Set();
    // LemonSlice avatar personas currently connected. When this drains
    // to empty after at least one connected, the session auto-ends —
    // no point staying on the session screen with no comedians left.
    this._connectedAvatars = new Set();
    this._everHadAvatar = false;

    // Cross-room control-event dedup. The agent fans every
    // `commentary.control` event out to every room stamped with a UUID
    // `event_id`; we subscribe on every controller
    // for redundancy, then collapse N copies down to one before any
    // handler sees it. Reset on each `_resetSessionUi` so a fresh
    // session starts with an empty cache.
    this._eventDeduper = new EventDeduper();
  }

  get state() {
    return this._state;
  }
  get activeTabId() {
    return this._activeTabId;
  }
  setActiveTabId(id) {
    this._activeTabId = id;
  }

  // ── Public lifecycle ──

  async start() {
    if (this._state !== SessionState.IDLE) return;
    const btn = $("#start-btn");
    const videoUrl = btn.dataset.videoUrl;
    const videoTitle = btn.dataset.videoTitle || "";
    if (!videoUrl) {
      showError("No active media tab detected");
      return;
    }

    this._state = SessionState.STARTING;
    btn.disabled = true;
    btn.classList.add("loading");
    btn.textContent = "Starting...";
    hideError();

    try {
      // Build the audio graph synchronously on the user gesture so the
      // AudioContext resume is gesture-tied. The graph also has to
      // exist before the first avatar track can arrive.
      await this._audio.init();

      const payload = await createSessionApi(videoUrl, videoTitle);

      // Same callback for every controller — first one to fire wins
      // and tears the whole session down.
      const onDisconnected = (info) => this._onRoomDisconnected(info);

      // Spawn one RoomController per entry in `rooms[]` and connect
      // them in parallel. Track-subscribed and track-unsubscribed are
      // wired on every controller so each room's persona-owned tracks
      // (audio + avatar video) flow into the same `avatar-slots`
      // registry / `AudioGraph` keyed by persona — no per-room slot
      // map. Data, active-speaker, and participant-lifecycle handlers
      // stay primary-only: the agent publishes commentary control on
      // the primary room and avatar auto-end is tracked off that
      // participant set.
      this._session = parseSession(payload);
      this._controllers = new Map();
      for (const [persona, entry] of this._session.rooms) {
        const isPrimary = entry.role === "primary";
        const controller = new RoomController({
          roomName: entry.roomName,
          token: entry.token,
          role: entry.role,
          persona,
          handlers: isPrimary
            ? this._buildHandlers(entry.roomName)
            : this._buildSecondaryHandlers(entry.roomName),
          onDisconnected,
        });
        this._controllers.set(persona, controller);
        if (isPrimary) {
          this._room = controller;
          this._primaryPersona = persona;
        }
      }

      $("#setup-screen").classList.add("hidden");
      $("#session-screen").classList.remove("hidden");

      // Fail-fast: if any controller's connect rejects, Promise.all
      // rejects and the catch below tears down whatever already
      // succeeded. Connection ordering doesn't matter, so don't
      // serialize.
      await Promise.all(
        [...this._controllers.values()].map((c) => c.connect(payload.livekit_url)),
      );

      // Tab audio publishes exactly once, to the primary room only.
      // Secondary rooms must not see this uplink — duplicating it would
      // double the user's outbound bandwidth and let secondary agents
      // react to the source audio they're meant to receive only via the
      // primary's relay.
      const primaryRoom = [...this._controllers.values()].find((c) => c.role === "primary").room;
      this._tabAudioStream = await captureAndPublishTabAudio({
        tabId: this._activeTabId,
        room: primaryRoom,
        audioGraph: this._audio,
      });

      this._state = SessionState.LIVE;
      console.log("[ext] Session started:", this._session.sessionId);
    } catch (err) {
      console.error("[ext] Failed to start session:", err);
      // Roll back any partial setup so a retry starts from a clean
      // slate. Without this, a publishTrack failure after room.connect
      // would leak the AudioContext, the captured MediaStream, and a
      // half-wired Room with stale event handlers.
      await this._teardownPartialStart();
      showError(friendlyApiError(err));
      this._resetSetupUi();
      this._state = SessionState.IDLE;
    }
  }

  async end() {
    // LIVE is the only state where teardown work needs to happen.
    // STARTING funnels through the failure path inside `start`;
    // ENDING is a teardown already in flight; IDLE has nothing to do.
    if (this._state !== SessionState.LIVE) return;
    this._state = SessionState.ENDING;

    const endBtn = $("#end-btn");
    if (endBtn) endBtn.disabled = true;

    try {
      // Teardown order:
      //   1. Stop tab capture so we don't keep streaming source audio
      //      into a room we're about to disconnect from.
      //   2. Dispose every remaining RoomController. Each dispose drops
      //      its listeners before issuing CLIENT_INITIATED disconnect,
      //      which prevents a teardown disconnect from re-entering
      //      `_onRoomDisconnected`.
      //   3. Tear down the audio graph (closes the AudioContext).
      //   4. Reset the UI back to the setup screen.
      stopTabStream(this._tabAudioStream);
      this._tabAudioStream = null;
      await this._disposeControllers();
      this._audio.teardown();
      this._session = null;
      this._resetSessionUi();
      this._resetSetupUi();
      // Re-detect media in the active tab so the start button reflects
      // current state (the page may have stopped playback while we were
      // mid-session).
      this._redetectActiveMedia();
    } finally {
      if (endBtn) endBtn.disabled = false;
      this._state = SessionState.IDLE;
    }
  }

  // Public hooks for the entry point.

  pauseFollower() {
    this._audio.stopFollower();
  }
  resumeFollower() {
    if (this._state === SessionState.LIVE) this._audio.startFollower();
  }

  skipCommentary() {
    if (this._speakingNow.size === 0) return;
    if (!this._sendPrimaryControl({ type: "skip" }, "podcast.control")) {
      // Primary is the only command channel up — without it we can't
      // reach the agent at all. Tear the session down (which disposes
      // every controller, primary and secondary) so the user lands back
      // on the setup screen with the error rather than staring at a
      // skip button that silently does nothing.
      this._failSafePrimaryLost();
    }
  }

  publishPacing() {
    const p = getPacing();
    this._sendPrimaryControl(
      { type: "settings", frequency: p.frequency, length: p.length },
      "podcast.control",
    );
  }

  // ── Internal helpers ──

  async _teardownPartialStart() {
    try {
      await this._disposeControllers();
    } catch (err) {
      console.warn("[ext] dispose during partial-start cleanup:", err);
    }
    try {
      this._audio.teardown();
    } catch (err) {
      console.warn("[ext] audio teardown during partial-start cleanup:", err);
    }
    stopTabStream(this._tabAudioStream);
    this._tabAudioStream = null;
    this._session = null;
    this._resetSessionUi();
  }

  // Dispose every controller we own, then clear the references. Used by
  // both clean teardown (`end`) and partial-start cleanup. Disposing
  // each controller handles the case where some connected and others
  // didn't (each controller's own dispose() is a no-op when its room is
  // null).
  async _disposeControllers() {
    if (!this._controllers) return;
    const controllers = [...this._controllers.values()];
    this._controllers = null;
    this._room = null;
    this._primaryPersona = null;
    await Promise.all(controllers.map((c) => c.dispose()));
  }

  // Build the SessionLifecycle event handler bag for the primary
  // controller. `roomName` is closed over so `_onTrackSubscribed` knows
  // which room delivered the track and can pass that through to the
  // `avatar-slots` registry's ownership guard.
  _buildHandlers(roomName) {
    return {
      onTrackSubscribed: (track, publication, participant) =>
        this._onTrackSubscribed(track, publication, participant, roomName),
      onTrackUnsubscribed: this._onTrackUnsubscribed.bind(this),
      onDataReceived: this._onDataReceived.bind(this),
      onActiveSpeakers: this._onActiveSpeakers.bind(this),
      onConnectionState: this._onConnectionState.bind(this),
      onParticipantConnected: this._onParticipantConnected.bind(this),
      onParticipantDisconnected: this._onParticipantDisconnected.bind(this),
    };
    // Note: disconnect handling is wired separately via the controller's
    // dedicated `onDisconnected` constructor param (see `start`) so it
    // fires uniformly on primary AND secondary rooms.
  }

  // Secondary controllers. Track-subscribed and track-unsubscribed are
  // wired so secondary-room avatar tracks land in the same
  // `avatar-slots` registry / `AudioGraph` as the primary's, keyed by
  // persona — the registry is the single shared seam between rooms
  // (no per-room slot map). Data is also wired so control events that
  // survived only on a secondary's data channel still reach
  // `_onDataReceived`; the EventDeduper collapses any duplicates before
  // downstream handlers fire. Active-speaker and
  // participant-lifecycle handlers stay primary-only and are omitted
  // here.
  _buildSecondaryHandlers(roomName) {
    return {
      onTrackSubscribed: (track, publication, participant) =>
        this._onTrackSubscribed(track, publication, participant, roomName),
      onTrackUnsubscribed: this._onTrackUnsubscribed.bind(this),
      onDataReceived: this._onDataReceived.bind(this),
    };
  }

  _resetSessionUi() {
    this._speakingNow.clear();
    this._introNow.clear();
    this._connectedAvatars.clear();
    this._everHadAvatar = false;
    this._eventDeduper.reset();
    resetAllSlots();
    this._updateSkipButton();
  }

  _resetSetupUi() {
    $("#session-screen").classList.add("hidden");
    $("#setup-screen").classList.remove("hidden");
    const btn = $("#start-btn");
    btn.disabled = false;
    btn.classList.remove("loading");
    btn.textContent = "Start Couchverse";
  }

  _redetectActiveMedia() {
    detectActiveMedia({
      onPreview: ({ url, title }) => {
        const btn = $("#start-btn");
        btn.disabled = false;
        btn.dataset.videoUrl = url;
        btn.dataset.videoTitle = title || "";
      },
      onNoMedia: () => {
        const btn = $("#start-btn");
        btn.disabled = true;
        delete btn.dataset.videoUrl;
        delete btn.dataset.videoTitle;
      },
    })
      .then((tabId) => {
        if (tabId != null) this._activeTabId = tabId;
      })
      .catch((err) => console.warn("[ext] re-detect after end failed:", err));
  }

  _updateSkipButton() {
    const btn = $("#skip-btn");
    if (!btn) return;
    // Disabled when nobody is mid-commentary OR when an intro is in
    // flight — intros are non-skippable so the Fox → Alien ritual
    // always plays out.
    btn.disabled = this._speakingNow.size === 0 || this._introNow.size > 0;
  }

  // ── Tab-bridge handlers (called from sidepanel.js entry) ──

  handleMediaStateUpdate(msg) {
    if (this._state !== SessionState.LIVE) return;
    if (!this._primaryController()?.isConnected()) return;
    // Pausing the media cuts the tab-audio track the agent relies on
    // for STT, so there's nothing left for Fox to react to. End the
    // session rather than leave the avatar streaming into silence.
    if (!msg.playing) {
      this.end().catch((err) => console.warn("[ext] auto-end on pause failed:", err));
      return;
    }
    this._sendPrimaryControl({ type: "play", t: msg.time }, "podcast.control");
  }

  // ── Room event handlers ──

  // `roomName` is forwarded by the per-controller handler bag (see
  // `_buildHandlers` / `_buildSecondaryHandlers`). Carrying it into
  // `mountAvatarVideo` lets the avatar-slots registry warn if a slot
  // is ever remounted from a different room than originally claimed
  // it — a defensive guard against a regression that would otherwise
  // silently overwrite one persona's video with another's.
  _onTrackSubscribed(track, publication, participant, roomName = null) {
    const { personaName, key } = resolvePersonaKey(participant, track, publication);

    if (track.kind === Track.Kind.Audio) {
      // Only attach tracks that resolve to a known persona — either by
      // LemonSlice avatar identity (`lemonslice-avatar-<name>`) or by
      // track name (`persona-<name>`). The sidechain follower takes the
      // peak RMS across every attached analyser, so any unrelated audio
      // track folded into the graph would over-duck the tab from a
      // signal that isn't actually persona voice.
      if (!personaName) {
        console.warn(
          "[ext] Ignoring unknown audio track:",
          "identity=",
          participant.identity,
          "trackName=",
          track.name || publication?.trackName,
        );
        return;
      }
      this._audio.attachPersona(track, key);
      return;
    }

    if (track.kind !== Track.Kind.Video) return;

    const isAvatarTrack =
      personaFromAvatarIdentity(participant.identity) !== null ||
      participant.attributes?.["lk.publish_on_behalf"];
    if (!isAvatarTrack) return;

    const slot = slotFor(personaName);
    if (!slot) return;
    mountAvatarVideo(slot, track, roomName);
    spawnReaction(slot, "eyes");
  }

  _onTrackUnsubscribed(track, publication, participant) {
    if (track.kind === Track.Kind.Audio) {
      const { key } = resolvePersonaKey(participant, track, publication);
      if (this._audio.hasPersona(key)) {
        this._audio.detachPersona(key);
        return;
      }
    }
    track.detach().forEach((el) => el.remove());
  }

  _onDataReceived(payload, _participant, _kind, topic) {
    let msg;
    try {
      msg = JSON.parse(new TextDecoder().decode(payload));
    } catch {
      return;
    }

    // Cross-room dedup: the agent fans the same event out to every room
    // and stamps each copy with a shared `event_id`. First arrival
    // wins; later copies on other rooms are dropped here so every
    // downstream branch fires exactly once per logical event.
    if (!this._eventDeduper.check(msg.event_id)) return;

    if (topic === "commentary.control" && msg.type === "agent_ready") {
      console.log("[ext] Agent ready — syncing playhead");
      this._syncPlayhead();
      this.publishPacing();
      return;
    }

    // Commentary lifecycle — drives UI state only (slot highlighting,
    // Skip button enable). Tab-audio ducking is NOT driven from here;
    // the sidechain envelope follower watches the actual persona
    // voice signal and decides for itself, so a late commentary_end
    // can't leave the tab stuck ducked.
    if (topic === "commentary.control" && msg.type === "commentary_start") {
      const personaName = msg.speaker;
      const phase = msg.phase || "commentary";
      if (personaName) {
        if (phase === "intro") this._introNow.add(personaName);
        else this._speakingNow.add(personaName);
        const slot = slotFor(personaName);
        slot?.classList.add("speaking");
        spawnReaction(slot, "random");
      }
      this._updateSkipButton();
      return;
    }

    if (topic === "commentary.control" && msg.type === "commentary_end") {
      const personaName = msg.speaker;
      const phase = msg.phase || "commentary";
      if (personaName) {
        if (phase === "intro") this._introNow.delete(personaName);
        else this._speakingNow.delete(personaName);
        // Only drop the "speaking" class if nobody from either set is
        // still mid-utterance for that persona.
        if (!this._speakingNow.has(personaName) && !this._introNow.has(personaName)) {
          setSlotSpeaking(personaName, false);
        }
      }
      this._updateSkipButton();
      return;
    }

  }

  // VAD-driven active-speaker updates highlight the matching slot only
  // when commentary.control hasn't already lit it. Purely visual
  // jitter is acceptable here; commentary_start/end remains the
  // authoritative source.
  _onActiveSpeakers(speakers) {
    const localId = this._room.localParticipantIdentity;
    const activePersonas = new Set();
    for (const p of speakers) {
      if (p.identity === localId) continue;
      const personaName = personaFromAvatarIdentity(p.identity);
      if (personaName) activePersonas.add(personaName);
    }
    for (const slot of document.querySelectorAll(".avatar-slot")) {
      const name = slot.dataset.name;
      const shouldHighlight = activePersonas.has(name) || this._speakingNow.has(name);
      slot.classList.toggle("speaking", shouldHighlight);
    }
  }

  _onConnectionState(state) {
    console.log("[ext] Connection state:", state);
  }

  // Fired when a controller's underlying Room emits RoomEvent.Disconnected.
  // LiveKit only emits this once its internal reconnect loop has given
  // up, so transient blips don't reach us — this signal always means
  // "the room is really gone." CLIENT_INITIATED is the disconnect we
  // ourselves initiated via `dispose()`; the listeners are normally
  // pulled before that fires, but we gate on the reason defensively in
  // case a future SDK update changes the teardown ordering. Anything
  // else (server kicked, signal failure, room deleted, agent crash) is
  // session-ending: the room's gone and we deliberately do NOT try to
  // reconnect — the user has to click Start again.
  _onRoomDisconnected({ reason, roomName, role, persona }) {
    if (reason === DisconnectReason.CLIENT_INITIATED) {
      console.log(
        "[ext] Room disconnected (client-initiated):",
        "name=", roomName, "role=", role, "persona=", persona,
      );
      return;
    }
    console.warn(
      "[ext] Room disconnected mid-session — ending session:",
      "name=", roomName, "role=", role, "persona=", persona, "reason=", reason,
    );
    // First disconnect wins. Subsequent calls (e.g. the second room
    // dropping moments later) hit the state guard inside `end()` and
    // become no-ops.
    if (this._state !== SessionState.LIVE) return;
    showError("Connection lost — please restart");
    this.end().catch((err) =>
      console.warn("[ext] auto-end on room disconnect failed:", err),
    );
  }

  _onParticipantConnected(participant) {
    const personaName = personaFromAvatarIdentity(participant.identity);
    if (!personaName) return;
    this._connectedAvatars.add(personaName);
    this._everHadAvatar = true;
  }

  // Once every avatar that joined this session has left, there's no
  // commentary coming — auto-end so the user lands back on the start
  // screen instead of an empty stage. The `_everHadAvatar` gate
  // prevents firing during the initial connect window before any
  // avatar has shown up. The state check avoids re-entering `end`
  // when the disconnect we're handling is itself fired by an
  // explicit teardown already in flight.
  _onParticipantDisconnected(participant) {
    const personaName = personaFromAvatarIdentity(participant.identity);
    if (!personaName) return;
    this._connectedAvatars.delete(personaName);
    if (
      this._state === SessionState.LIVE &&
      this._everHadAvatar &&
      this._connectedAvatars.size === 0
    ) {
      this.end().catch((err) => console.warn("[ext] auto-end after avatars left failed:", err));
    }
  }

  _syncPlayhead() {
    syncPlayheadToAgent({
      tabId: this._activeTabId,
      onPlay: ({ t }) => this._sendPrimaryControl({ type: "play", t }, "podcast.control"),
      onPause: () => this._sendPrimaryControl({ type: "pause" }, "podcast.control"),
    });
  }

  // Resolve the primary RoomController via the controllers map so the
  // primary-only contract is enforced from a single source of truth.
  // Returns null when no session is live.
  _primaryController() {
    if (!this._controllers || !this._primaryPersona) return null;
    return this._controllers.get(this._primaryPersona) ?? null;
  }

  // Single chokepoint for outbound user commands. Always publishes via
  // the primary controller — never iterates `_controllers`. The agent
  // fans the command out to its in-process secondary state, so a
  // duplicate from the extension would race the agent's authoritative
  // ordering. Returns true when sent, false when the primary is gone
  // (caller decides whether that's a no-op or a fail-safe trigger).
  _sendPrimaryControl(payload, topic) {
    const primary = this._primaryController();
    if (!primary || !primary.isConnected()) return false;
    primary.publishControl(payload, topic);
    return true;
  }

  // Primary-room-disconnected fail-safe for user-initiated controls.
  // Tears the whole session down (so any still-connected secondary is
  // disposed by `_disposeControllers` rather than left dangling) and
  // surfaces an error on the setup screen the user lands back on.
  _failSafePrimaryLost() {
    showError("Lost connection to commentary — please restart");
    this.end().catch((err) =>
      console.warn("[ext] auto-end on lost primary failed:", err),
    );
  }
}
