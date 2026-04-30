/**
 * RoomController — owns a single LiveKit Room instance and its event
 * handlers. Encapsulates the connect/dispose lifecycle so callers can't
 * accidentally publish on a half-torn-down room or forget to remove
 * listeners before disconnect (the closures would otherwise hold stale
 * references to the per-session state).
 *
 * Each instance is bound to one room at construction time
 * (`{roomName, token, role, persona}`). Under the dual-room architecture
 * `SessionLifecycle` instantiates one per entry in the API's `rooms[]`
 * response and connects them in parallel.
 *
 * Event handlers are passed in under `handlers` — callers wire their own
 * concerns (audio routing, UI state, captions) without RoomController
 * needing to know what they do. Each room is given the subset of
 * handlers it actually needs: the primary room owns the data channel and
 * publishes tab audio, while secondary rooms only subscribe to their
 * persona's avatar track.
 */

import { ConnectionState, Room, RoomEvent } from "livekit-client";

export class RoomController {
  constructor({ roomName, token, role, persona, handlers = {}, onDisconnected = null } = {}) {
    this._roomName = roomName;
    this._token = token;
    this._role = role;
    this._persona = persona;
    this._handlers = handlers;
    // First-class "this room died" notification, separate from the
    // per-event handler bag. Wired for every controller — primary and
    // secondary — so SessionLifecycle can enforce the rule that either
    // room dropping ends the whole session. Dispose() removes
    // listeners before the explicit disconnect, so this callback only
    // fires for non-CLIENT_INITIATED disconnects we didn't ask for.
    this._onDisconnected = onDisconnected;
    this._room = null;
  }

  get roomName() {
    return this._roomName;
  }
  get role() {
    return this._role;
  }
  get persona() {
    return this._persona;
  }

  async connect(livekitUrl) {
    this._room = new Room({ adaptiveStream: true, dynacast: true });

    const h = this._handlers;
    const wire = (event, handler) => handler && this._room.on(event, handler);
    wire(RoomEvent.TrackSubscribed, h.onTrackSubscribed);
    wire(RoomEvent.TrackUnsubscribed, h.onTrackUnsubscribed);
    wire(RoomEvent.DataReceived, h.onDataReceived);
    wire(RoomEvent.ActiveSpeakersChanged, h.onActiveSpeakers);
    wire(RoomEvent.ConnectionStateChanged, h.onConnectionState);
    wire(RoomEvent.ParticipantConnected, h.onParticipantConnected);
    wire(RoomEvent.ParticipantDisconnected, h.onParticipantDisconnected);

    if (this._onDisconnected) {
      this._room.on(RoomEvent.Disconnected, (reason) => {
        this._onDisconnected({
          reason,
          roomName: this._roomName,
          role: this._role,
          persona: this._persona,
        });
      });
    }

    await this._room.connect(livekitUrl, this._token);
    console.log(
      "[ext] Connected to LiveKit room",
      "name=", this._roomName,
      "role=", this._role,
      "persona=", this._persona,
    );
  }

  async dispose() {
    if (!this._room) return;
    const prior = this._room;
    this._room = null;
    // Drop our handlers before disconnecting so the closures over the
    // now-null room and the participant sets don't fire on the
    // teardown's own disconnect events.
    try {
      prior.removeAllListeners();
    } catch {}
    try {
      // `disconnect(true)` waits for the LiveKit transport to actually
      // close — awaiting it prevents a new Start from racing with the
      // old room's teardown.
      await prior.disconnect(true);
    } catch (err) {
      console.warn("[ext] room.disconnect raised:", err);
    }
  }

  isConnected() {
    return this._room?.state === ConnectionState.Connected;
  }

  get room() {
    return this._room;
  }

  get localParticipantIdentity() {
    return this._room?.localParticipant?.identity ?? null;
  }

  async publishTrack(mediaStreamTrack, options) {
    if (!this._room) throw new Error("Room not connected");
    return this._room.localParticipant.publishTrack(mediaStreamTrack, options);
  }

  // Best-effort fire-and-forget JSON publish on a topic. Silently
  // no-ops when not connected so callers (pacing UI, skip button) don't
  // need to gate every call site.
  async publishControl(payload, topic) {
    if (!this.isConnected()) return;
    try {
      const encoder = new TextEncoder();
      await this._room.localParticipant.publishData(encoder.encode(JSON.stringify(payload)), {
        reliable: true,
        topic,
      });
    } catch (err) {
      console.warn("[ext] publishData failed:", err);
    }
  }
}
