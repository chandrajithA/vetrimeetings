/**
 * webrtc.js — Vetri Meetings
 *
 * Key fixes in this version:
 *   • initRoom() retries enableCameraAndMicrophone() up to 3× with
 *     increasing back-off when the error is NotReadableError (device busy).
 *     This handles the race where the preview stream hasn't fully released
 *     the hardware yet despite track.stop() having been called.
 *   • _syncLocalTrackStates() is only called after a successful open so it
 *     never incorrectly marks cam/mic as "off" due to a transient error.
 *   • If all retries fail the error is surfaced cleanly and buttons reflect
 *     the real state (both off).
 */

const { Room, RoomEvent, Track, ConnectionState, createLocalVideoTrack } = LivekitClient;

let room = null;
let videoEnabled       = true;
let audioEnabled       = true;
let screenShareEnabled = false;

const PAGE_SIZE = 6;
let currentPage = 0;
let allParticipants = [];

let sidebarPage = 0;
function getSidebarPageSize() { return window.innerWidth <= 640 ? 2 : 4; }

let facingMode    = "user";
let flipInProgress = false;

let mediaRecorder  = null;
let recordedChunks = [];
let isRecording    = false;

let activeScreenShareIdentity = null;

let unreadCount = 0;
let chatOpen    = false;

let participantStatuses = {};
let meetingEndedByHost  = false;

//////////////////////////////////////////////////////
// 🔁  BUTTON IMAGE HELPERS
//////////////////////////////////////////////////////

function setBtnIcon(imgId, isOn) {
    const img = document.getElementById(imgId);
    if (!img) return;
    const src = isOn ? img.dataset.srcOn : img.dataset.srcOff;
    if (src) img.src = src;
}

function setFlipIcon(mode) {
    const img = document.getElementById("flipBtnImg");
    if (!img) return;
    img.src = (mode === "user") ? img.dataset.srcFront : img.dataset.srcBack;
}

//////////////////////////////////////////////////////
// 🪞  MIRROR HELPER
//////////////////////////////////////////////////////

function applyMirror(videoEl, shouldMirror) {
    if (!videoEl) return;
    videoEl.style.transform = shouldMirror ? "scaleX(-1)" : "none";
    // CRITICAL: This only affects local display CSS.
    // The MediaStreamTrack frames sent to LiveKit are NOT affected by CSS transforms.
    // If remote peers still see mirrored video, the camera hardware is sending
    // mirrored frames and a canvas-based flip is needed on the outgoing track.
}

let _canvasMirrorStream = null;
let _canvasMirrorStop   = null;

function _stopCanvasMirror() {
    if (_canvasMirrorStop) { _canvasMirrorStop(); _canvasMirrorStop = null; }
    _canvasMirrorStream = null;
}

async function _getUnmirroredTrack(constraints) {
    _stopCanvasMirror();

    // Get raw camera stream
    const rawStream = await navigator.mediaDevices.getUserMedia({ video: constraints, audio: false });
    const rawTrack  = rawStream.getVideoTracks()[0];
    const settings  = rawTrack.getSettings();
    const W = settings.width  || 1280;
    const H = settings.height || 720;

    // Create offscreen canvas that flips the frame back
    const canvas  = document.createElement("canvas");
    canvas.width  = W;
    canvas.height = H;
    const ctx = canvas.getContext("2d");

    const srcVideo = document.createElement("video");
    srcVideo.srcObject = rawStream;
    srcVideo.muted = true;
    srcVideo.playsInline = true;
    await srcVideo.play();

    let stopped = false;
    function drawFrame() {
        if (stopped) return;
        // Flip horizontally: mirror back so remote sees correct orientation
        ctx.save();
        ctx.translate(W, 0);
        ctx.scale(-1, 1);
        ctx.drawImage(srcVideo, 0, 0, W, H);
        ctx.restore();
        requestAnimationFrame(drawFrame);
    }
    drawFrame();

    // Capture the canvas as a stream
    const canvasStream = canvas.captureStream(30);
    const canvasTrack  = canvasStream.getVideoTracks()[0];

    _canvasMirrorStop = () => {
        stopped = true;
        rawTrack.stop();
        srcVideo.srcObject = null;
    };

    return canvasTrack;
}

//////////////////////////////////////////////////////
// 🚀  INIT
//////////////////////////////////////////////////////

async function initRoom(opts = {}) {
    const startMicOn     = opts.startMicOn     !== undefined ? opts.startMicOn     : true;
    const startCamOn     = opts.startCamOn     !== undefined ? opts.startCamOn     : true;
    const previewStream  = opts.previewStream  || null;   // ← live tracks from lobby

    audioEnabled = startMicOn;
    videoEnabled = startCamOn;

    try {
        const res = await fetch(`/meeting/token/${ROOM_NAME}/`);
        if (!res.ok) throw new Error("Failed to get token");
        const { token, livekit_url } = await res.json();

        room = new Room({
            adaptiveStream: true,
            dynacast: true,
            reconnectPolicy: { maxRetries: 5, retryDelayMs: 300 },
            videoCaptureDefaults: {
                resolution: { width: 1280, height: 720, frameRate: 30 },
                facingMode: "user",
            },
        });

        attachRoomEvents();
        await room.connect(livekit_url, token, { autoSubscribe: true });
        console.log("Connected to LiveKit:", ROOM_NAME);

        const local = room.localParticipant;
        addParticipant(local.identity);
        addParticipantTile(local, true);
        _setParticipantStatus(local.identity, local.name || "You", videoEnabled, audioEnabled, true);

        /* ── Publish tracks ──────────────────────────────────────────────────
           Strategy A (preferred): wrap the still-live preview MediaStreamTrack
             directly — zero re-acquisition, no NotReadableError possible.
           Strategy B (fallback): create fresh tracks if no preview stream was
             passed (e.g. direct URL load) or a particular track is missing.
           ──────────────────────────────────────────────────────────────────── */
        let avOpened = false;

        async function _publishFromMediaTrack(mediaTrack, kind) {
            try {
                mediaTrack.enabled = true;  // always re-enable before publishing

                // Verify track is still alive
                if (mediaTrack.readyState !== "live") {
                    console.warn(`${kind} track is ${mediaTrack.readyState}, skipping`);
                    return false;
                }

                let lkTrack;
                const publishOptions = {};

                if (kind === "video") {
                    lkTrack = new LivekitClient.LocalVideoTrack(mediaTrack, undefined, false);
                    publishOptions.source = LivekitClient.Track.Source.Camera;
                    publishOptions.videoCodec = "vp8";
                    publishOptions.simulcast = false;
                } else {
                    lkTrack = new LivekitClient.LocalAudioTrack(mediaTrack, undefined, false);
                    publishOptions.source = LivekitClient.Track.Source.Microphone;
                }

                await local.publishTrack(lkTrack, publishOptions);
                return true;
            } catch(e) {
                console.error(`publishFromMediaTrack(${kind}) failed:`, e.name, e.message);
                try { mediaTrack.stop(); } catch(_) {}
                return false;
            }
        }

        async function _acquireAndPublish(kind) {
            const backoffs = [0, 600, 1200, 2000];
            for (let i = 0; i < backoffs.length; i++) {
                if (backoffs[i] > 0) await new Promise(r => setTimeout(r, backoffs[i]));
                try {
                    const track = kind === "video"
                        ? await LivekitClient.createLocalVideoTrack({ facingMode: "user", resolution: { width: 1280, height: 720, frameRate: 30 } })
                        : await LivekitClient.createLocalAudioTrack();
                    await local.publishTrack(track);
                    return true;
                } catch(err) {
                    const denied = err.name === "NotAllowedError" || err.name === "PermissionDeniedError";
                    if (denied) { console.warn(`${kind} permission denied`); return false; }
                    if (i < backoffs.length - 1) { console.warn(`${kind} fresh attempt ${i+1} failed (${err.name}), retrying…`); continue; }
                    console.error(`${kind} failed after all retries:`, err.name, err.message);
                    return false;
                }
            }
            return false;
        }

        // Camera
        if (startCamOn) {
            const previewVideo = previewStream?.getVideoTracks()?.[0];
            if (previewVideo && previewVideo.readyState === "live") {
                videoEnabled = await _publishFromMediaTrack(previewVideo, "video");
            } else {
                // No usable preview track — acquire fresh (lobby had cam off, or stream missing)
                videoEnabled = await _acquireAndPublish("video");
            }
        } else {
            videoEnabled = false;
        }

        // Microphone
        if (startMicOn) {
            const previewAudio = previewStream?.getAudioTracks()?.[0];
            if (previewAudio && previewAudio.readyState === "live") {
                audioEnabled = await _publishFromMediaTrack(previewAudio, "audio");
            } else {
                audioEnabled = await _acquireAndPublish("audio");
            }
        } else {
            audioEnabled = false;
        }

        // Stop any leftover preview tracks we didn't use
        if (previewStream) {
            previewStream.getTracks().forEach(t => {
                const camPub = local.getTrackPublication(LivekitClient.Track.Source.Camera);
                const micPub = local.getTrackPublication(LivekitClient.Track.Source.Microphone);
                const inUse  = camPub?.track?.mediaStreamTrack === t || micPub?.track?.mediaStreamTrack === t;
                if (!inUse) t.stop();
            });
        }

        avOpened = videoEnabled || audioEnabled;

        /* Only sync real track states when we successfully opened at least something */
        if (avOpened) {
            _syncLocalTrackStates(local);
        } else {
            /* Reflect denied/failed state in UI */
            _applyAvState(local.identity, false, false);
        }

        attachLocalTracks(local);

        /* Sync control-bar icons with final state */
        setBtnIcon("audioBtnImg", audioEnabled);
        setBtnIcon("videoBtnImg", videoEnabled);
        setBtnIcon("screenShareBtnImg", true);
        setBtnIcon("recordBtnImg", true);
        const flipBtn = document.getElementById("flipBtn");
        if (flipBtn && /Mobi|Android|iPhone|iPad/i.test(navigator.userAgent)) {
            flipBtn.style.display = videoEnabled ? "flex" : "none";
        }
        setFlipIcon("user");
        document.getElementById("audioBtn")?.classList.toggle("active", !audioEnabled);
        document.getElementById("videoBtn")?.classList.toggle("active", !videoEnabled);

        /* Update participant panel with real states */
        if (participantStatuses[local.identity]) {
            participantStatuses[local.identity].videoOn = videoEnabled;
            participantStatuses[local.identity].audioOn = audioEnabled;
            renderParticipantsPanel();
        }

        /* Existing remote participants */
        room.remoteParticipants.forEach(participant => {
            addParticipant(participant.identity);
            addParticipantTile(participant, false);
            _setParticipantStatus(participant.identity, participant.name || participant.identity, true, true, false);
            participant.trackPublications.forEach(pub => {
                if (pub.isSubscribed && pub.track) {
                    if (pub.track.source === Track.Source.ScreenShare) _mountScreenShare(pub.track, participant);
                    else attachTrackToTile(pub.track, participant);
                }
            });
        });

        renderPage();
        updateParticipantCount();
        _broadcastLocalState();

    } catch (err) {
        console.error("Room init error:", err);
        showError("Could not connect: " + err.message);
    }
}

/* Called when tracks opened successfully — reads actual grant status */
function _syncLocalTrackStates(participant) {
    const camPub = participant.getTrackPublication(Track.Source.Camera);
    const micPub = participant.getTrackPublication(Track.Source.Microphone);

    const camGranted = !!(camPub?.track);
    const micGranted = !!(micPub?.track);

    /* Intersect with user intent — only override to false, never to true */
    videoEnabled = videoEnabled && camGranted;
    audioEnabled = audioEnabled && micGranted;

    _applyAvState(participant.identity, videoEnabled, audioEnabled);
}

/* Push state to DOM without touching LiveKit tracks */
function _applyAvState(identity, camOn, audioOn) {
    const overlay = document.querySelector(`#tile-${identity} .video-off-overlay`);
    const muteIcon = document.querySelector(`#tile-${identity} .mute-icon`);

    if (!camOn)   overlay?.classList.remove("hidden");
    else          overlay?.classList.add("hidden");

    if (!audioOn) muteIcon?.classList.remove("hidden");
    else          muteIcon?.classList.add("hidden");

    if (participantStatuses[identity]) {
        participantStatuses[identity].videoOn = camOn;
        participantStatuses[identity].audioOn = audioOn;
        renderParticipantsPanel();
    }
}

//////////////////////////////////////////////////////
// 👤  PARTICIPANT STATUS
//////////////////////////////////////////////////////

function _setParticipantStatus(identity, name, videoOn, audioOn, isLocal) {
    participantStatuses[identity] = { name: name || identity, videoOn: !!videoOn, audioOn: !!audioOn, isLocal: !!isLocal };
    renderParticipantsPanel();
}
function _removeParticipantStatus(identity) { delete participantStatuses[identity]; renderParticipantsPanel(); }

//////////////////////////////////////////////////////
// 🧑‍🤝‍🧑  PARTICIPANTS PANEL
//////////////////////////////////////////////////////

function renderParticipantsPanel() {
    const list    = document.getElementById("participantsList");
    const countEl = document.getElementById("panelParticipantCount");
    if (!list) return;

    const entries = Object.entries(participantStatuses);
    if (countEl) countEl.textContent = entries.length;
    list.innerHTML = "";

    entries.sort(([, a], [, b]) => {
        if (a.isLocal && !b.isLocal) return -1;
        if (!a.isLocal && b.isLocal) return 1;
        return a.name.localeCompare(b.name);
    });

    const icons = (typeof ICONS !== "undefined") ? ICONS : {};

    entries.forEach(([identity, status]) => {
        const row = document.createElement("div");
        row.className = "p-row"; row.id = "p-row-" + identity;

        const avatar = document.createElement("div");
        avatar.className = "p-avatar";
        avatar.textContent = getInitials(status.name);

        const nameEl = document.createElement("div");
        nameEl.className = "p-name";
        nameEl.textContent = status.isLocal ? `${status.name} (You)` : status.name;

        const iconsEl = document.createElement("div");
        iconsEl.className = "p-icons";

        const camIcon = document.createElement("img");
        camIcon.className = "p-status-icon " + (status.videoOn ? "p-video-on" : "p-video-off");
        camIcon.src = status.videoOn ? (icons.camOn || "") : (icons.camOff || "");
        camIcon.alt = status.videoOn ? "Camera on" : "Camera off";
        camIcon.title = camIcon.alt;

        const micIcon = document.createElement("img");
        micIcon.className = "p-status-icon " + (status.audioOn ? "p-audio-on" : "p-audio-off");
        micIcon.src = status.audioOn ? (icons.micOn || "") : (icons.micOff || "");
        micIcon.alt = status.audioOn ? "Mic on" : "Mic off";
        micIcon.title = micIcon.alt;

        iconsEl.appendChild(camIcon);
        iconsEl.appendChild(micIcon);
        row.appendChild(avatar); row.appendChild(nameEl); row.appendChild(iconsEl);
        list.appendChild(row);
    });
}

//////////////////////////////////////////////////////
// 📡  ROOM EVENTS
//////////////////////////////////////////////////////

function attachRoomEvents() {

    room.on(RoomEvent.ParticipantConnected, (p) => {
        addParticipant(p.identity); addParticipantTile(p, false);
        _setParticipantStatus(p.identity, p.name || p.identity, true, true, false);
        renderPage(); updateParticipantCount();
        _broadcastLocalState();   // ← ADD THIS LINE — tell the new joiner our state
    });

    room.on(RoomEvent.ParticipantDisconnected, (p) => {
        removeParticipant(p.identity); removeTileDOM(p.identity); _removeParticipantStatus(p.identity);
        if (activeScreenShareIdentity === p.identity) _destroyScreenShare();
        renderPage(); updateParticipantCount();
    });

    room.on(RoomEvent.TrackSubscribed, (track, pub, participant) => {
        if (track.source === Track.Source.ScreenShare) {
            _mountScreenShare(track, participant);
        } else {
            if (!document.getElementById("tile-" + participant.identity)) {
                addParticipant(participant.identity);
                addParticipantTile(participant, false);   // ← false is correct here
                _setParticipantStatus(participant.identity, participant.name || participant.identity, true, true, false);
                renderPage();
            }
            attachTrackToTile(track, participant);   // ← this calls applyMirror(video, false) ✓
        }
    });

    room.on(RoomEvent.TrackUnsubscribed, (track) => {
        track.detach();
        if (track.source === Track.Source.ScreenShare) _destroyScreenShare();
    });

    room.on(RoomEvent.LocalTrackPublished, (pub, participant) => {
        if (pub.source === Track.Source.Camera) attachLocalTracks(participant);  
        // attachLocalTracks calls applyMirror(video, true) — correct for LOCAL tile
    });

    room.on(RoomEvent.LocalTrackUnpublished, (pub) => {
        if (pub.source === Track.Source.ScreenShare) {
            screenShareEnabled = false;
            document.getElementById("screenShareBtn")?.classList.remove("active");
            setBtnIcon("screenShareBtnImg", true);
            if (activeScreenShareIdentity) _destroyScreenShare();
        }
    });

    room.on(RoomEvent.TrackMuted, (pub, participant) => {
        const tile    = document.getElementById("tile-" + participant.identity);
        const isLocal = participant.identity === room.localParticipant?.identity;
        if (pub.source === Track.Source.Camera) {
            // For local: we manage this manually — don't override
            if (!isLocal) {
                tile?.querySelector(".video-off-overlay")?.classList.remove("hidden");
                if (participantStatuses[participant.identity]) {
                    participantStatuses[participant.identity].videoOn = false;
                    renderParticipantsPanel();
                }
            }
        }
        if (pub.source === Track.Source.Microphone) {
            tile?.querySelector(".mute-icon")?.classList.remove("hidden");
            if (participantStatuses[participant.identity]) {
                participantStatuses[participant.identity].audioOn = false;
                renderParticipantsPanel();
            }
            if (isLocal) {
                audioEnabled = false;
                setBtnIcon("audioBtnImg", false);
                document.getElementById("audioBtn")?.classList.add("active");
            }
        }
    });

    room.on(RoomEvent.TrackUnmuted, (pub, participant) => {
        const tile    = document.getElementById("tile-" + participant.identity);
        const isLocal = participant.identity === room.localParticipant?.identity;
        if (pub.source === Track.Source.Camera) {
            // For local: we manage this manually — don't override
            if (!isLocal) {
                tile?.querySelector(".video-off-overlay")?.classList.add("hidden");
                const p2 = participant.getTrackPublication(Track.Source.Camera);
                if (p2?.videoTrack) {
                    const v = document.getElementById("video-" + participant.identity);
                    if (v) { p2.videoTrack.detach(v); p2.videoTrack.attach(v); applyMirror(v, false); }
                }
                if (participantStatuses[participant.identity]) {
                    participantStatuses[participant.identity].videoOn = true;
                    renderParticipantsPanel();
                }
            }
        }
        if (pub.source === Track.Source.Microphone) {
            tile?.querySelector(".mute-icon")?.classList.add("hidden");
            if (participantStatuses[participant.identity]) {
                participantStatuses[participant.identity].audioOn = true;
                renderParticipantsPanel();
            }
            if (isLocal) {
                audioEnabled = true;
                setBtnIcon("audioBtnImg", true);
                document.getElementById("audioBtn")?.classList.remove("active");
            }
        }
    });

    room.on(RoomEvent.ActiveSpeakersChanged, (speakers) => {
        document.querySelectorAll(".video-tile").forEach(t => t.classList.remove("speaking"));
        speakers.forEach(s => document.getElementById("tile-" + s.identity)?.classList.add("speaking"));
    });

    room.on(RoomEvent.ConnectionStateChanged, (state) => {
        if (state === ConnectionState.Reconnecting) {
            // Only show reconnecting if it's NOT a host-ended scenario
            if (!meetingEndedByHost) showStatus("Reconnecting…");
        } else if (state === ConnectionState.Connected) {
            hideStatus();
        } else if (state === ConnectionState.Disconnected) {
            if (meetingEndedByHost) {
                // Both host and non-host: ensure redirect fires
                // (may already be navigating, this is a safety net)
                setTimeout(() => { window.location.href = "/"; }, 1000);
            }
            // Non-host, no meetingEndedByHost = real network drop — don't redirect
        }
    });

    room.on(RoomEvent.RoomMetadataChanged, (metadata) => {
        if (metadata === "ended") {
            meetingEndedByHost = true;
            if (!IS_HOST) {
                // Stop non-host hardware immediately when metadata arrives
                room.localParticipant.trackPublications.forEach(pub => {
                    const mst = pub.track?.mediaStreamTrack;
                    if (mst) mst.stop();
                });
                _showMeetingEndedToast("Meeting ended by the host. Redirecting…");
                setTimeout(() => { window.location.href = "/"; }, 2000);
            }
        }
    });

    room.on(RoomEvent.DataReceived, (payload, participant) => {
        try {
            const msg = JSON.parse(new TextDecoder().decode(payload));
            if (msg.type === "chat")
                appendChatMessage(participant?.name || "Someone", msg.text, false);
            if (msg.type === "mute_all" && !IS_HOST && audioEnabled)
                toggleAudio();
            if (msg.type === "recording_started")
                showRecordingBanner(msg.recorder_name || "Someone");
            if (msg.type === "recording_stopped")
                hideRecordingBanner();

            // ── Video off/on for remote tile ────────────────────────────────
            if (msg.type === "cam_state") {
                const tile = document.getElementById("tile-" + msg.identity);
                if (tile) {
                    tile.querySelector(".video-off-overlay")?.classList.toggle("hidden", msg.on);
                }
                if (participantStatuses[msg.identity]) {
                    participantStatuses[msg.identity].videoOn = msg.on;
                    renderParticipantsPanel();
                }
            }

            // ── Mic off/on for remote tile ──────────────────────────────────
            if (msg.type === "mic_state") {
                const tile = document.getElementById("tile-" + msg.identity);
                if (tile) {
                    tile.querySelector(".mute-icon")?.classList.toggle("hidden", msg.on);
                }
                if (participantStatuses[msg.identity]) {
                    participantStatuses[msg.identity].audioOn = msg.on;
                    renderParticipantsPanel();
                }
            }
        } catch(e) {}
    });

    window.addEventListener("resize", () => { sidebarPage = 0; renderPage(); });
}

function _showMeetingEndedToast(msg) {
    let toast = document.getElementById("meetingEndedToast");
    if (!toast) {
        toast = document.createElement("div"); toast.id = "meetingEndedToast";
        toast.style.cssText = "position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:rgba(20,22,32,0.97);color:#e8eaf0;padding:24px 36px;border-radius:16px;font-size:15px;font-weight:500;z-index:99999;text-align:center;border:1px solid rgba(255,255,255,0.1)";
        document.body.appendChild(toast);
    }
    toast.innerHTML = `<div style="font-size:28px;margin-bottom:12px">📵</div>${escapeHtml(msg)}`;
}

//////////////////////////////////////////////////////
// 🖥️   SCREEN SHARE
//////////////////////////////////////////////////////

function _mountScreenShare(track, participant) {
    if (activeScreenShareIdentity === participant.identity) return;
    _destroyScreenShare(false);
    activeScreenShareIdentity = participant.identity;
    const tile = document.createElement("div");
    tile.id = "screenshare-tile"; tile.className = "video-tile screenshare-tile";
    const video = document.createElement("video");
    video.id = "screenshare-video"; video.autoplay = true; video.playsInline = true; video.muted = true;
    video.style.transform = "none";
    const label = document.createElement("div");
    label.className = "video-name screenshare-label";
    label.innerText = `${participant.name || participant.identity} is sharing screen`;
    tile.appendChild(video); tile.appendChild(label);
    document.getElementById("videoContainer").prepend(tile);
    track.attach(video);
    sidebarPage = 0; renderPage();
}

function _destroyScreenShare(doRender = true) {
    activeScreenShareIdentity = null;
    const tile = document.getElementById("screenshare-tile");
    if (!tile) return;
    tile.querySelectorAll("video").forEach(v => { v.pause(); v.srcObject = null; });
    tile.remove();
    if (doRender) renderPage();
}

//////////////////////////////////////////////////////
// 👥  PARTICIPANT LIST
//////////////////////////////////////////////////////

function addParticipant(identity) { if (!allParticipants.includes(identity)) allParticipants.push(identity); }
function removeParticipant(identity) {
    allParticipants = allParticipants.filter(id => id !== identity);
    const tp = Math.ceil(allParticipants.length / PAGE_SIZE);
    if (currentPage >= tp) currentPage = Math.max(0, tp - 1);
}

//////////////////////////////////////////////////////
// 📄  LAYOUT & PAGINATION
//////////////////////////////////////////////////////

function renderPage() {
    const container      = document.getElementById("videoContainer");
    const hasScreenShare = !!document.getElementById("screenshare-tile");
    if (hasScreenShare) {
        const isMobile = window.innerWidth <= 640;
        container.className = "video-container layout-screenshare" + (isMobile ? " layout-screenshare-mobile" : "");
        const pageSize = getSidebarPageSize();
        const totalSP  = Math.max(1, Math.ceil(allParticipants.length / pageSize));
        sidebarPage = Math.min(sidebarPage, totalSP - 1);
        const visibleIds = allParticipants.slice(sidebarPage * pageSize, (sidebarPage + 1) * pageSize);
        allParticipants.forEach(id => {
            const tile = document.getElementById("tile-" + id); if (!tile) return;
            if (visibleIds.includes(id)) { tile.style.display = ""; tile.classList.add("sidebar-tile"); }
            else { tile.style.display = "none"; tile.classList.remove("sidebar-tile"); }
        });
        updateSidebarPagination(totalSP); updatePagination(false); return;
    }
    allParticipants.forEach(id => { const t = document.getElementById("tile-"+id); if (t) t.classList.remove("sidebar-tile"); });
    const nav = document.getElementById("sidebarPagination"); if (nav) nav.style.display = "none";
    const totalPages = Math.max(1, Math.ceil(allParticipants.length / PAGE_SIZE));
    currentPage = Math.min(currentPage, totalPages - 1);
    const pageIds = allParticipants.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE);
    allParticipants.forEach(id => { const t = document.getElementById("tile-"+id); if (!t) return; t.style.display = pageIds.includes(id) ? "" : "none"; });
    updatePagination(totalPages > 1, totalPages);
    updateLayout(pageIds.length);
}

function changePage(dir) { const tp = Math.ceil(allParticipants.length / PAGE_SIZE); currentPage = Math.max(0, Math.min(tp-1, currentPage+dir)); renderPage(); }
function changeSidebarPage(dir) { const ps = getSidebarPageSize(); const tp = Math.ceil(allParticipants.length/ps); sidebarPage = Math.max(0, Math.min(tp-1, sidebarPage+dir)); renderPage(); }

function updatePagination(show, totalPages = 1) {
    const pag = document.getElementById("pagination"); const dots = document.getElementById("pageDots");
    if (!pag) return;
    if (!show) { pag.classList.add("hidden"); return; }
    pag.classList.remove("hidden"); dots.innerHTML = "";
    for (let i = 0; i < totalPages; i++) {
        const dot = document.createElement("div");
        dot.className = "page-dot" + (i === currentPage ? " active" : "");
        dot.onclick = () => { currentPage = i; renderPage(); };
        dots.appendChild(dot);
    }
}

function updateSidebarPagination(totalPages) {
    let nav = document.getElementById("sidebarPagination"); const isMobile = window.innerWidth <= 640;
    if (totalPages <= 1) { if (nav) nav.style.display = "none"; return; }
    if (!nav) { nav = document.createElement("div"); nav.id = "sidebarPagination"; nav.className = "sidebar-pagination"; document.getElementById("videoContainer").appendChild(nav); }
    nav.style.display = "flex";
    nav.innerHTML = isMobile
        ? `<button class="sidebar-arrow" onclick="changeSidebarPage(-1)" ${sidebarPage===0?"disabled":""}>&#8249;</button><span class="sidebar-page-info">${sidebarPage+1}/${totalPages}</span><button class="sidebar-arrow" onclick="changeSidebarPage(1)" ${sidebarPage>=totalPages-1?"disabled":""}>&#8250;</button>`
        : `<button class="sidebar-arrow" onclick="changeSidebarPage(-1)" ${sidebarPage===0?"disabled":""}>&#8679;</button><span class="sidebar-page-info">${sidebarPage+1}/${totalPages}</span><button class="sidebar-arrow" onclick="changeSidebarPage(1)" ${sidebarPage>=totalPages-1?"disabled":""}>&#8681;</button>`;
}

//////////////////////////////////////////////////////
// 🎥  TILE MANAGEMENT
//////////////////////////////////////////////////////

function addParticipantTile(participant, isLocal) {
    removeTileDOM(participant.identity);
    const tile = document.createElement("div");
    tile.className = "video-tile"; tile.id = "tile-" + participant.identity;
    const video = document.createElement("video");
    video.id = "video-" + participant.identity; video.autoplay = true; video.playsInline = true; video.muted = isLocal;
    applyMirror(video, isLocal);
    const overlay = document.createElement("div");
    overlay.className = "video-off-overlay hidden";
    overlay.innerHTML = `<div class="avatar">${getInitials(participant.name)}</div>`;
    const nameLabel = document.createElement("div");
    nameLabel.className = "video-name";
    nameLabel.innerText = isLocal ? `${participant.name || "You"} (You)` : (participant.name || participant.identity);
    const muteIcon = document.createElement("div");
    muteIcon.className = "mute-icon hidden"; muteIcon.innerText = "🔇";
    tile.appendChild(video); tile.appendChild(overlay); tile.appendChild(nameLabel); tile.appendChild(muteIcon);
    document.getElementById("videoContainer").appendChild(tile);
}

function attachLocalTracks(participant) {
    const video = document.getElementById("video-" + participant.identity);
    if (!video) return;
    const camPub = participant.getTrackPublication(Track.Source.Camera);
    const lkTrack = camPub?.videoTrack ?? camPub?.track;
    if (lkTrack) {
        try { lkTrack.detach(video); } catch(e) {}
        lkTrack.attach(video);
    }
    // Only mirror front-facing camera, never rear
    const isFront = facingMode === "user";
    applyMirror(video, isFront);
}

function attachTrackToTile(track, participant) {
    if (track.kind === Track.Kind.Video) {
        const video = document.getElementById("video-" + participant.identity);
        if (video) {
            track.detach(video);
            track.attach(video);
            applyMirror(video, false);   // always false for remote — never mirror
        }
    }
    if (track.kind === Track.Kind.Audio) track.attach();
}

function removeTileDOM(identity) {
    const tile = document.getElementById("tile-" + identity); if (!tile) return;
    tile.querySelectorAll("video, audio").forEach(el => { el.srcObject = null; el.remove(); });
    tile.remove();
}

//////////////////////////////////////////////////////
// 📐  LAYOUT
//////////////////////////////////////////////////////

function updateLayout(visibleCount) {
    const container = document.getElementById("videoContainer");
    const hasScreenShare = !!document.getElementById("screenshare-tile");
    if (visibleCount === undefined) {
        visibleCount = [...container.querySelectorAll(".video-tile:not(#screenshare-tile)")]
            .filter(t => t.style.display !== "none").length;
    }
    container.className = "video-container";
    if (hasScreenShare)        { container.classList.add("layout-screenshare"); return; }
    if      (visibleCount <= 1) container.classList.add("layout-1");
    else if (visibleCount <= 2) container.classList.add("layout-2");
    else if (visibleCount <= 4) container.classList.add("layout-4");
    else if (visibleCount <= 6) container.classList.add("layout-6");
    else if (visibleCount <= 9) container.classList.add("layout-9");
    else                        container.classList.add("layout-16");
}

function updateParticipantCount() {
    const el = document.getElementById("participantCount");
    if (el) el.innerText = (room?.remoteParticipants?.size || 0) + 1;
}

//////////////////////////////////////////////////////
// 🎥  TOGGLE CAMERA
//////////////////////////////////////////////////////

async function toggleVideo() {
    if (!room) return;
    videoEnabled = !videoEnabled;
    const identity = room.localParticipant.identity;
    const overlay  = document.querySelector(`#tile-${identity} .video-off-overlay`);

    // ── Flip button: only show when video is ON and on mobile ─────────────
    const flipBtn = document.getElementById("flipBtn");
    if (flipBtn && /Mobi|Android|iPhone|iPad/i.test(navigator.userAgent)) {
        flipBtn.style.display = videoEnabled ? "flex" : "none";
    }

    if (!videoEnabled) {
        // TURN OFF: stop hardware immediately
        const camPub = room.localParticipant.getTrackPublication(Track.Source.Camera);
        if (camPub?.track) {
            const mst = camPub.track.mediaStreamTrack;
            if (mst) mst.stop();
            room.localParticipant.unpublishTrack(camPub.track, true).catch(e =>
                console.warn("unpublishTrack cam-off:", e.message)
            );
        }
        overlay?.classList.remove("hidden");
        _broadcastVideoMuteState(false);

    } else {
        // TURN ON: acquire fresh track, show locally immediately
        overlay?.classList.add("hidden");
        try {
            const track = await LivekitClient.createLocalVideoTrack({
                facingMode: facingMode,
                resolution: { width: 1280, height: 720, frameRate: 30 },
            });

            // Force un-mirror the outgoing track using applyConstraints
            try {
                await track.mediaStreamTrack.applyConstraints({
                    advanced: [{ zoom: 1 }]   // triggers constraint refresh on some browsers
                });
            } catch(_) {}

            // Apply CSS mirror ONLY on local tile — remote track stays un-mirrored
            const videoEl = document.getElementById("video-" + identity);
            if (videoEl) {
                try { track.detach(videoEl); } catch(_) {}
                track.attach(videoEl);
                applyMirror(videoEl, true);   // CSS only, doesn't affect outgoing frames
            }
            room.localParticipant.publishTrack(track, {
                source: Track.Source.Camera,
                videoCodec: "vp8",
                simulcast: false,
            }).catch(e => console.error("publishTrack cam-on:", e.name, e.message));

            _broadcastVideoMuteState(true);
        } catch(e) {
            console.error("toggleVideo re-acquire failed:", e.name, e.message);
            videoEnabled = false;
            overlay?.classList.remove("hidden");
            if (flipBtn && /Mobi|Android|iPhone|iPad/i.test(navigator.userAgent)) {
                flipBtn.style.display = "none";
            }
        }
    }

    setBtnIcon("videoBtnImg", videoEnabled);
    document.getElementById("videoBtn")?.classList.toggle("active", !videoEnabled);
    if (participantStatuses[identity]) {
        participantStatuses[identity].videoOn = videoEnabled;
        renderParticipantsPanel();
    }
}

function _broadcastVideoMuteState(isOn) {
    if (!room) return;
    try {
        const msg = { type: "cam_state", identity: room.localParticipant.identity, on: isOn };
        room.localParticipant.publishData(
            new TextEncoder().encode(JSON.stringify(msg)),
            { reliable: true }
        );
    } catch(e) {}
}

function _broadcastLocalState() {
    if (!room) return;
    // Small delay so the new participant's data channel is ready to receive
    setTimeout(() => {
        _broadcastVideoMuteState(videoEnabled);
        _broadcastAudioMuteState(audioEnabled);
    }, 800);
}


//////////////////////////////////////////////////////
// 🔄  FLIP CAMERA
//////////////////////////////////////////////////////

async function flipCamera() {
    if (!room || flipInProgress) return;
    flipInProgress = true;
    const newFacing = (facingMode === "user") ? "environment" : "user";
    const btn = document.getElementById("flipBtn");
    btn?.classList.add("active");

    const attempt = async () => {
        const camPub = room.localParticipant.getTrackPublication(Track.Source.Camera);
        if (camPub?.track) await room.localParticipant.unpublishTrack(camPub.track, true);
        await new Promise(r => setTimeout(r, 300));
        const newTrack = await createLocalVideoTrack({ facingMode: newFacing, resolution: { width: 1280, height: 720, frameRate: 30 } });
        await room.localParticipant.publishTrack(newTrack);
        const video = document.getElementById("video-" + room.localParticipant.identity);
        if (video) { newTrack.detach(video); newTrack.attach(video); applyMirror(video, true); }
        return true;
    };

    try {
        let ok = false;
        try { ok = await attempt(); }
        catch(e) { console.warn("Flip attempt 1 failed, retrying…", e.message); await new Promise(r => setTimeout(r, 600)); ok = await attempt(); }
        if (ok) { facingMode = newFacing; setFlipIcon(facingMode); }
    } catch(e) {
        console.error("Camera flip failed:", e); setFlipIcon(facingMode);
        showStatus("Camera flip failed."); setTimeout(hideStatus, 2000);
    } finally {
        flipInProgress = false; setTimeout(() => btn?.classList.remove("active"), 400);
    }
}

//////////////////////////////////////////////////////
// 🎤  TOGGLE MIC
//////////////////////////////////////////////////////

async function toggleAudio() {
    if (!room) return;
    audioEnabled = !audioEnabled;
    const identity = room.localParticipant.identity;

    if (!audioEnabled) {
        // TURN OFF: stop hardware immediately
        const micPub = room.localParticipant.getTrackPublication(Track.Source.Microphone);
        if (micPub?.track) {
            const mst = micPub.track.mediaStreamTrack;
            if (mst) mst.stop();
            room.localParticipant.unpublishTrack(micPub.track, true).catch(e =>
                console.warn("unpublishTrack mic-off:", e.message)
            );
        }
        _broadcastAudioMuteState(false);   // ← tell all peers mic is off

    } else {
        // TURN ON: acquire fresh track
        try {
            const track = await LivekitClient.createLocalAudioTrack();
            room.localParticipant.publishTrack(track, {
                source: Track.Source.Microphone,
            }).catch(e => console.error("publishTrack mic-on:", e.name, e.message));
            _broadcastAudioMuteState(true);   // ← tell all peers mic is on
        } catch(e) {
            console.error("toggleAudio re-acquire failed:", e.name, e.message);
            audioEnabled = false;
        }
    }

    document.querySelector(`#tile-${identity} .mute-icon`)?.classList.toggle("hidden", audioEnabled);
    setBtnIcon("audioBtnImg", audioEnabled);
    document.getElementById("audioBtn")?.classList.toggle("active", !audioEnabled);
    if (participantStatuses[identity]) {
        participantStatuses[identity].audioOn = audioEnabled;
        renderParticipantsPanel();
    }
}


function _broadcastAudioMuteState(isOn) {
    if (!room) return;
    try {
        const msg = { type: "mic_state", identity: room.localParticipant.identity, on: isOn };
        room.localParticipant.publishData(
            new TextEncoder().encode(JSON.stringify(msg)),
            { reliable: true }
        );
    } catch(e) {}
}

//////////////////////////////////////////////////////
// 🖥️  SCREEN SHARE
//////////////////////////////////////////////////////

async function toggleScreenShare() {
    if (!room) return;

    // Only block non-hosts on mobile — hosts can always try
    if (!IS_HOST && /iPhone|Android.*Mobile/i.test(navigator.userAgent) && !screenShareEnabled) {
        showStatus("Screen share is not supported on mobile.");
        setTimeout(hideStatus, 3000);
        return;
    }

    const btn = document.getElementById("screenShareBtn");
    try {
        if (!screenShareEnabled) {
            await room.localParticipant.setScreenShareEnabled(true);
        } else {
            await room.localParticipant.setScreenShareEnabled(false);
            screenShareEnabled = false;
            btn?.classList.remove("active");
            setBtnIcon("screenShareBtnImg", true);
        }
    } catch(e) {
        screenShareEnabled = false;
        btn?.classList.remove("active");
        setBtnIcon("screenShareBtnImg", true);

        // Give a more specific error on mobile
        const isMobile = /Mobi|Android|iPhone|iPad/i.test(navigator.userAgent);
        if (isMobile) {
            showStatus("Screen share not supported on this device/browser.");
        } else {
            showStatus("Screen share cancelled or denied.");
        }
        setTimeout(hideStatus, 2500);
    }
}

//////////////////////////////////////////////////////
// ⏺️  RECORDING
//////////////////////////////////////////////////////

async function toggleRecording() {
    const btn      = document.getElementById("recordBtn");
    const isMobile = /Mobi|Android|iPhone|iPad/i.test(navigator.userAgent);

    if (!isRecording) {
        try {
            let combinedStream;

            if (isMobile) {
                // Mobile host: record own camera + mic (no screen capture API available)
                combinedStream = await navigator.mediaDevices.getUserMedia({
                    video: { facingMode: "user", width: 1280, height: 720 },
                    audio: true
                });
                showStatus("📱 Recording camera (screen capture unavailable on mobile)");
                setTimeout(hideStatus, 3000);

            } else {
                // Desktop: record screen + mic
                const displayStream = await navigator.mediaDevices.getDisplayMedia({
                    video: { mediaSource: "screen" },
                    audio: true
                });
                let mergedStream = displayStream;
                try {
                    const micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
                    const audioCtx  = new AudioContext();
                    const dest      = audioCtx.createMediaStreamDestination();
                    audioCtx.createMediaStreamSource(displayStream).connect(dest);
                    audioCtx.createMediaStreamSource(micStream).connect(dest);
                    mergedStream = new MediaStream([
                        ...displayStream.getVideoTracks(),
                        ...dest.stream.getAudioTracks()
                    ]);
                } catch(e) {}
                combinedStream = mergedStream;

                // Stop recording if user closes screen share picker
                displayStream.getVideoTracks()[0].onended = () => {
                    if (isRecording) toggleRecording();
                };
            }

            recordedChunks = [];
            const mimeType = MediaRecorder.isTypeSupported("video/webm;codecs=vp9")
                ? "video/webm;codecs=vp9"
                : "video/webm";

            mediaRecorder = new MediaRecorder(combinedStream, { mimeType });

            mediaRecorder.ondataavailable = e => {
                if (e.data.size > 0) recordedChunks.push(e.data);
            };

            mediaRecorder.onstop = async () => {
                const blob = new Blob(recordedChunks, { type: "video/webm" });
                const fd   = new FormData();
                fd.append("recording", blob, `meeting-${ROOM_NAME}-${Date.now()}.webm`);
                fd.append("room_name", ROOM_NAME);
                showStatus("⏫ Uploading recording…");
                try {
                    const res  = await fetch("/meeting/save-recording/", {
                        method: "POST",
                        headers: { "X-CSRFToken": getCookie("csrftoken") },
                        body: fd
                    });
                    const data = await res.json();
                    if (data.url) showStatusHTML(`✅ Saved! <a href="${data.url}" target="_blank" rel="noopener" style="color:#7df;text-decoration:underline;">▶ View</a>`);
                    else showStatus("❌ Upload failed: " + (data.error || "Unknown"));
                } catch(err) {
                    showStatus("❌ Upload error: " + err.message);
                }
                recordedChunks = [];
            };

            mediaRecorder.start(1000);
            isRecording = true;
            btn?.classList.add("active");
            setBtnIcon("recordBtnImg", false);
            showRecordingBanner("You");
            _broadcastRecordingState(true);
            if (!isMobile) { showStatus("🔴 Recording started…"); setTimeout(hideStatus, 2000); }

        } catch(e) {
            console.error("Recording failed:", e);
            showStatus("Recording cancelled or not supported.");
            setTimeout(hideStatus, 2500);
        }

    } else {
        // STOP recording
        mediaRecorder?.stop();
        mediaRecorder?.stream?.getTracks().forEach(t => t.stop());
        isRecording = false;
        btn?.classList.remove("active");
        setBtnIcon("recordBtnImg", true);
        hideRecordingBanner();
        _broadcastRecordingState(false);
    }
}

async function _broadcastRecordingState(started) {
    if (!room) return;
    const msg = started ? { type:"recording_started", recorder_name: room.localParticipant.name||"Someone" } : { type:"recording_stopped" };
    await room.localParticipant.publishData(new TextEncoder().encode(JSON.stringify(msg)), { reliable:true });
}

function showRecordingBanner(r) {
    let b = document.getElementById("recordingBanner");
    if (!b) { b = document.createElement("div"); b.id = "recordingBanner"; b.style.cssText = "position:fixed;top:12px;left:50%;transform:translateX(-50%);background:rgba(180,0,0,0.88);color:#fff;padding:7px 18px;border-radius:20px;font-size:13px;font-weight:600;z-index:9999;display:flex;align-items:center;gap:8px;backdrop-filter:blur(4px);pointer-events:none"; document.body.appendChild(b); }
    b.innerHTML = `<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#ff3333;animation:recPulse 1s infinite"></span>${escapeHtml(r)} is recording this meeting`;
    b.style.display = "flex";
}
function hideRecordingBanner() { const b = document.getElementById("recordingBanner"); if (b) b.style.display = "none"; }

//////////////////////////////////////////////////////
// 💬  CHAT
//////////////////////////////////////////////////////

async function sendChatMessage(text) {
    if (!room || !text.trim()) return;
    await room.localParticipant.publishData(new TextEncoder().encode(JSON.stringify({ type:"chat", text })), { reliable:true });
    appendChatMessage("You", text, true);
}

function appendChatMessage(sender, text, isOwn) {
    const chat = document.getElementById("chatMessages"); if (!chat) return;
    const msg = document.createElement("div");
    msg.className = "chat-message " + (isOwn ? "chat-own" : "chat-other");
    msg.innerHTML = `<span class="chat-sender">${escapeHtml(sender)}</span><span class="chat-text">${escapeHtml(text)}</span>`;
    chat.appendChild(msg); chat.scrollTop = chat.scrollHeight;
    if (!isOwn && !chatOpen) { unreadCount++; _updateChatBadge(); }
}

function _updateChatBadge() {
    let badge = document.getElementById("chatBadge");
    if (!badge) { const btn = document.getElementById("chatBtn"); if (!btn) return; badge = document.createElement("span"); badge.id = "chatBadge"; badge.className = "chat-badge hidden"; btn.appendChild(badge); }
    if (unreadCount > 0) { badge.textContent = unreadCount > 99 ? "99+" : String(unreadCount); badge.classList.remove("hidden"); }
    else badge.classList.add("hidden");
}

function _onChatOpened() { chatOpen = true; unreadCount = 0; _updateChatBadge(); }
function _onChatClosed() { chatOpen = false; }

//////////////////////////////////////////////////////
// 🚪  LEAVE / END
//////////////////////////////////////////////////////

async function leaveMeeting() { if (isRecording) toggleRecording(); if (room) await room.disconnect(); window.location.href = "/"; }

async function endMeeting() {
    if (!IS_HOST || !room) return;

    // 1. Stop recording if active
    if (isRecording) toggleRecording();

    // 2. Stop ALL local hardware tracks immediately — camera + mic light off NOW
    room.localParticipant.trackPublications.forEach(pub => {
        const mst = pub.track?.mediaStreamTrack;
        if (mst) mst.stop();
    });

    // 3. Set flag so ConnectionStateChanged redirect fires for host too
    meetingEndedByHost = true;

    // 4. Show toast to host immediately
    _showMeetingEndedToast("Ending meeting…");

    // 5. Call backend (no await — fire and forget, don't block UI)
    fetch(`/meeting/end/${ROOM_NAME}/`, {
        method: "POST",
        headers: { "X-CSRFToken": getCookie("csrftoken") }
    }).catch(e => console.warn("end_meeting fetch failed:", e));

    // 6. Disconnect host from room immediately — don't wait for backend
    try { await room.disconnect(); } catch(e) {}

    // 7. Navigate host to dashboard
    window.location.href = "/";
}

async function muteAll() {
    if (!IS_HOST || !room) return;
    await room.localParticipant.publishData(new TextEncoder().encode(JSON.stringify({ type:"mute_all" })), { reliable:true });
}

//////////////////////////////////////////////////////
// 🔧  HELPERS
//////////////////////////////////////////////////////

function getInitials(name) { if (!name) return "?"; return name.split(" ").map(n=>n[0]).join("").toUpperCase().slice(0,2); }
function escapeHtml(t) { return String(t).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }
function getCookie(name) { const v = document.cookie.match("(^|;)\\s*"+name+"\\s*=\\s*([^;]+)"); return v?v.pop():""; }
function showError(msg)       { const el=document.getElementById("statusBar"); if(el){el.textContent=msg;el.className="status-bar error";el.classList.remove("hidden");} }
function showStatus(msg)      { const el=document.getElementById("statusBar"); if(el){el.textContent=msg;el.className="status-bar info";el.classList.remove("hidden");} }
function showStatusHTML(html) { const el=document.getElementById("statusBar"); if(el){el.innerHTML=html;el.className="status-bar info";el.classList.remove("hidden");} }
function hideStatus()         { const el=document.getElementById("statusBar"); if(el) el.className="status-bar hidden"; }


