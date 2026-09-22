"""Signalling and session server."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import secrets
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from aiohttp import web
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

from .capture import PROFILES, FrameSource, SwitchableSource, display_geometry, target_size
from .inputs import InputController
from .media import ScreenTrack, prefer_h264, tune_encoder
from .relay import (
    TAG_AUDIO,
    TAG_CONFIG,
    TAG_INPUT,
    TAG_READY,
    TAG_VIDEO,
    AnnexBEncoder,
    RelayProtocolError,
    RelaySession,
)
from . import clipboard
from .security import AuthError, SessionAuth

logger = logging.getLogger(__name__)

AudioSink = Callable[[bytes], Awaitable[None]]

WEB_ROOT = Path(__file__).parent / "web"
SESSION_COOKIE = "machop_session"

CLIPBOARD_SETTLE_SECONDS = 0.18

RELAY_IDLE_SECONDS = 0.25
RELAY_REFINE_FRAMES = 6

RELAY_VIEWPORT_GRACE_SECONDS = 1.0
RELAY_RESIZE_SETTLE_SECONDS = 2.0

RELAY_BLOCKED_HIGH = 0.5
RELAY_BLOCKED_LOW = 0.2
RELAY_MIN_FPS = 5.0
RELAY_MAX_FPS = 30.0
RELAY_ADAPT_WINDOW = 0.5
RELAY_STALL_SECONDS = 0.25

AUDIO_CHANNEL_BACKLOG = 256_000

MIN_CAPTURE_WIDTH = 640
VIEWPORT_RESIZE_SLOP = 64

DEFAULT_ICE_SERVERS = [
    "stun:stun.l.google.com:19302",
    "stun:stun1.l.google.com:19302",
    "stun:stun.cloudflare.com:3478",
]


@dataclass
class ServerState:
    auth: SessionAuth
    source: FrameSource
    controller: InputController
    ice_servers: list[str] = field(default_factory=lambda: list(DEFAULT_ICE_SERVERS))
    relay_bitrate: int = 4_000_000
    relay_enabled: bool = True
    audio_allowed: bool = True
    audio_on: bool = False
    relay_keyframe_wanted: bool = False
    viewer_hidden: bool = False
    max_width: int = 1440
    profile: str = "gui"
    min_bitrate: int | None = None
    viewport_width: int | None = None
    peers: set[RTCPeerConnection] = field(default_factory=set)
    on_state_change: Callable[[str], None] | None = None
    on_viewer_active: Callable[[], None] | None = None
    on_viewer_idle: Callable[[], None] | None = None
    on_relay_active: Callable[[bool], None] | None = None


class SignalingServer:
    def __init__(
        self,
        auth: SessionAuth,
        source: FrameSource,
        controller: InputController,
        ice_servers: list[str] | None = None,
        relay_bitrate: int = 4_000_000,
        relay_enabled: bool = True,
        audio_allowed: bool = True,
        max_width: int = 1440,
        profile: str = "gui",
        min_bitrate: int | None = None,
        on_state_change: Callable[[str], None] | None = None,
        on_viewer_active: Callable[[], None] | None = None,
        on_viewer_idle: Callable[[], None] | None = None,
        on_relay_active: Callable[[bool], None] | None = None,
    ) -> None:
        self._viewers = 0
        self._evicting = False
        self._audio_source = None
        self._audio_encoder = None
        self._audio_task: asyncio.Task | None = None
        self._audio_sinks: set[AudioSink] = set()
        self._audio_lock = asyncio.Lock()
        self._viewport_seen = asyncio.Event()
        self._requested_size: tuple[int, int] | None = None
        self.state = ServerState(
            auth=auth,
            source=source,
            controller=controller,
            relay_bitrate=relay_bitrate,
            relay_enabled=relay_enabled,
            audio_allowed=audio_allowed,
            max_width=max_width,
            profile=profile if profile in PROFILES else "gui",
            min_bitrate=min_bitrate,
            on_viewer_active=on_viewer_active,
            on_viewer_idle=on_viewer_idle,
            on_relay_active=on_relay_active,
            ice_servers=list(DEFAULT_ICE_SERVERS) if ice_servers is None else list(ice_servers),
            on_state_change=on_state_change,
        )
        self.app = web.Application()
        self.app.router.add_get("/", self._index)
        self.app.router.add_get("/health", self._health)
        self.app.router.add_post("/offer", self._offer)
        self.app.router.add_get("/relay", self._relay)
        self.app.router.add_static("/static/", WEB_ROOT, name="static")
        self.app.on_shutdown.append(self._on_shutdown)


    async def _index(self, request: web.Request) -> web.StreamResponse:
        response = web.FileResponse(WEB_ROOT / "index.html")
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "peers": len(self.state.peers)})

    async def _offer(self, request: web.Request) -> web.Response:
        try:
            params = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "Malformed request"}, status=400)

        sdp = params.get("sdp")
        offer_type = params.get("type")
        if not isinstance(sdp, str) or offer_type != "offer":
            return web.json_response({"error": "Malformed offer"}, status=400)
        if "m=video" not in sdp:
            return web.json_response(
                {"error": "Offer contained no video media"}, status=400
            )

        cookie = request.cookies.get(SESSION_COOKIE)
        session_id = cookie or secrets.token_urlsafe(18)
        pin = str(params.get("pin", ""))
        try:
            if not pin and cookie:
                self.state.auth.resume(cookie)
                displaced = False
            else:
                displaced = self.state.auth.authenticate(pin, session_id)
        except AuthError as exc:
            headers = {}
            if exc.retry_after:
                headers["Retry-After"] = str(int(exc.retry_after) + 1)
            return web.json_response(
                {"error": str(exc)}, status=exc.status, headers=headers
            )

        if displaced:
            await self._evict_peers()

        try:
            answer = await self._negotiate(
                RTCSessionDescription(sdp=sdp, type=offer_type)
            )
        except Exception:
            self.state.auth.release()
            raise
        response = web.json_response(answer)
        forwarded_proto = request.headers.get("X-Forwarded-Proto", "")
        over_tls = forwarded_proto == "https" or request.scheme == "https"
        response.set_cookie(
            SESSION_COOKIE,
            session_id,
            httponly=True,
            samesite="Strict",
            secure=over_tls,
            max_age=86400,
        )
        return response


    @property
    def viewer_count(self) -> int:
        return self._viewers

    def _capture_width(self) -> int:
        """The widest capture worth producing right now."""
        profile_max, _bitrate = PROFILES[self.state.profile]
        capped = min(profile_max, self.state.max_width)
        viewport = self.state.viewport_width
        if viewport:
            capped = min(capped, max(viewport, MIN_CAPTURE_WIDTH))
        return capped

    def _resize(self, reason: str) -> bool:
        """Re-derive the capture size from profile + viewport and apply it."""
        source = self.state.source
        if not isinstance(source, SwitchableSource):
            logger.warning("This capture source cannot change resolution")
            return False
        width, height = target_size(display_geometry(), self._capture_width())
        if (width, height) == (source.width, source.height):
            return True
        self._requested_size = (width, height)
        asyncio.create_task(source.reconfigure(width, height), name="resize")
        logger.info("Capture %dx%d (%s)", width, height, reason)
        return True

    def apply_profile(self, name: str) -> bool:
        """Switch capture resolution and encoder ceiling."""
        profile = PROFILES.get(name)
        if profile is None:
            logger.warning("Ignoring unknown quality profile %r", name)
            return False
        if not isinstance(self.state.source, SwitchableSource):
            logger.warning("This capture source cannot change resolution")
            return False
        self.state.profile = name
        tune_encoder(profile[1], self.state.min_bitrate)
        return self._resize(f"profile {name}")

    def _apply_viewport(self, width: int, height: int) -> bool:
        """Match the capture to the pixels the viewer can actually show."""
        if not (0 < width <= 8192 and 0 < height <= 8192):
            logger.warning("Ignoring implausible viewport %sx%s", width, height)
            return False
        current = self.state.viewport_width
        if current is not None and abs(current - width) < VIEWPORT_RESIZE_SLOP:
            return False
        self.state.viewport_width = width
        return self._resize(f"viewport {width}px")

    def _viewer_joined(self) -> None:
        """Fires on the FIRST viewer only: the display is already awake for the second, and a second assertion would leak."""
        self._viewers += 1
        if self._viewers == 1 and self.state.on_viewer_active is not None:
            self.state.on_viewer_active()

    def _viewer_left(self) -> None:
        if self._viewers == 0:
            return
        self._viewers -= 1
        if self._viewers:
            return
        if not self._evicting:
            self.state.auth.release()
        self.state.viewer_hidden = False
        self.state.relay_keyframe_wanted = False
        self._audio_sinks.clear()
        self._schedule_audio_stop()
        self.state.controller.release_all()
        if self.state.on_viewer_idle is not None:
            self.state.on_viewer_idle()


    async def _set_audio(self, on: bool, reply: Callable[[str], None] | None) -> None:
        """Start or stop sharing the Mac's sound, and say what happened."""
        detail = ""
        async with self._audio_lock:
            if on and not self.state.audio_allowed:
                detail = "sound sharing is turned off on the Mac (--no-audio)"
            elif on and self._audio_source is None:
                try:
                    await self._start_audio()
                except Exception as exc:
                    detail = str(exc) or type(exc).__name__
                    logger.warning("Could not start system audio: %s", detail)
                    await self._stop_audio()
            elif not on:
                await self._stop_audio()
            self.state.audio_on = self._audio_source is not None
        logger.info("Sound %s", "on" if self.state.audio_on else "off")
        if reply is not None:
            reply(json.dumps({"t": "au", "on": self.state.audio_on, "m": detail}))

    async def _start_audio(self) -> None:
        from .audio import OpusEncoder, SystemAudioSource

        source = SystemAudioSource()
        await source.start()
        self._audio_source = source
        self._audio_encoder = OpusEncoder()
        self._audio_task = asyncio.create_task(self._pump_audio(), name="audio")

    async def _stop_audio(self) -> None:
        task, self._audio_task = self._audio_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        source, self._audio_source = self._audio_source, None
        if source is not None:
            await source.stop()
        encoder, self._audio_encoder = self._audio_encoder, None
        if encoder is not None:
            encoder.close()
        self.state.audio_on = False

    def _schedule_audio_stop(self) -> None:
        """Stop sound from a synchronous caller (a viewer going away)."""
        if self._audio_source is None and self._audio_task is None:
            return

        async def run() -> None:
            async with self._audio_lock:
                await self._stop_audio()

        with contextlib.suppress(RuntimeError):
            asyncio.create_task(run(), name="audio-stop")

    async def _pump_audio(self) -> None:
        """Encode captured sound and hand it to every live transport."""
        source, encoder = self._audio_source, self._audio_encoder
        if source is None or encoder is None:
            return
        try:
            while True:
                block = await source.read()
                if not self._audio_sinks:
                    continue
                for timestamp, packet in encoder.encode(block):
                    payload = timestamp.to_bytes(8, "big") + packet
                    for sink in list(self._audio_sinks):
                        try:
                            await sink(payload)
                        except Exception:
                            self._audio_sinks.discard(sink)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Audio pump stopped")


    async def _relay(self, request: web.Request) -> web.WebSocketResponse:
        """Encrypted media over the tunnel, for when peer-to-peer ICE fails."""
        if not self.state.relay_enabled:
            raise web.HTTPForbidden(reason="Relay fallback is disabled")

        ws = web.WebSocketResponse(heartbeat=20.0, max_msg_size=4 << 20)
        await ws.prepare(request)

        session_id = request.cookies.get(SESSION_COOKIE) or secrets.token_urlsafe(18)
        try:
            hello = await asyncio.wait_for(ws.receive_json(), timeout=10.0)
        except (asyncio.TimeoutError, TypeError, ValueError, json.JSONDecodeError):
            await ws.close(code=4400, message=b"bad handshake")
            return ws

        try:
            displaced = self.state.auth.authenticate(
                str(hello.get("pin", "")), session_id
            )
        except AuthError as exc:
            await ws.close(code=4401, message=str(exc).encode()[:120])
            return ws
        if displaced:
            await self._evict_peers()

        crypto = RelaySession(self.state.auth.pin, role="server")
        try:
            confirm = crypto.establish(base64.b64decode(hello.get("pub", "")))
        except (RelayProtocolError, ValueError) as exc:
            self.state.auth.release()
            await ws.close(code=4400, message=str(exc).encode()[:120])
            return ws

        source = self.state.source
        await ws.send_json({
            "pub": base64.b64encode(crypto.public_key_bytes).decode(),
            "salt": base64.b64encode(crypto.salt).decode(),
            "confirm": base64.b64encode(confirm).decode(),
            "width": source.width,
            "height": source.height,
        })

        self.state.viewer_hidden = False
        self._viewport_seen.clear()
        try:
            width, height = int(hello.get("w", 0)), int(hello.get("h", 0))
        except (TypeError, ValueError):
            width = height = 0
        if width and height:
            self._apply_viewport(width, height)
            self._viewport_seen.set()
        forced = bool(hello.get("forced"))
        logger.info(
            "Relay engaged (%s)",
            "requested by the viewer" if forced else "peer-to-peer unavailable",
        )
        if self.state.on_relay_active is not None:
            self.state.on_relay_active(forced)
        self._viewer_joined()

        async def audio_sink(payload: bytes) -> None:
            await ws.send_bytes(crypto.seal(TAG_AUDIO, payload))

        self._audio_sinks.add(audio_sink)
        pump: asyncio.Task | None = None
        try:
            async for message in ws:
                if message.type is not web.WSMsgType.BINARY:
                    continue
                try:
                    tag, payload = crypto.open(message.data)
                except Exception as exc:
                    logger.warning("Dropping undecryptable relay record: %s", exc)
                    continue
                if tag == TAG_READY:
                    if pump is None:
                        pump = asyncio.create_task(
                            self._pump_video(ws, crypto), name="relay-video"
                        )
                elif tag == TAG_INPUT:
                    def reply(answer: str, ws=ws, crypto=crypto) -> None:
                        asyncio.create_task(
                            ws.send_bytes(crypto.seal(TAG_INPUT, answer.encode())),
                            name="relay-reply",
                        )

                    self._handle_input(payload.decode("utf-8", "replace"), reply)
        finally:
            self._audio_sinks.discard(audio_sink)
            if pump is not None:
                pump.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pump
            self._viewer_left()
        return ws

    async def _pump_video(self, ws: web.WebSocketResponse, crypto: RelaySession) -> None:
        """Encode and send frames until the socket closes."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                self._viewport_seen.wait(), timeout=RELAY_VIEWPORT_GRACE_SECONDS
            )
        wanted = self._requested_size
        if wanted is not None:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + RELAY_RESIZE_SETTLE_SECONDS
            while loop.time() < deadline:
                if (self.state.source.width, self.state.source.height) == wanted:
                    break
                await asyncio.sleep(0.05)

        source = self.state.source
        encoder: AnnexBEncoder | None = None
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="machop-encode")
        announced = False
        last_frame = None
        idle_sent = 0
        loop = asyncio.get_running_loop()
        allowed_fps = RELAY_MAX_FPS
        blocked = 0.0
        window_start = loop.time()
        last_sent_at = 0.0
        try:
            while not ws.closed:
                try:
                    frame = await asyncio.wait_for(
                        source.read(), timeout=RELAY_IDLE_SECONDS
                    )
                    last_frame = frame
                    idle_sent = 0
                    if self.state.viewer_hidden:
                        continue
                except asyncio.TimeoutError:
                    if last_frame is None:
                        continue
                    if self.state.viewer_hidden:
                        continue
                    if not (
                        self.state.relay_keyframe_wanted
                        or idle_sent < RELAY_REFINE_FRAMES
                    ):
                        continue
                    idle_sent += 1
                    frame = last_frame
                if encoder is None or (frame.width, frame.height) != (
                    encoder.width,
                    encoder.height,
                ):
                    if encoder is not None:
                        logger.info(
                            "Relay stream resizing %dx%d -> %dx%d",
                            encoder.width, encoder.height, frame.width, frame.height,
                        )
                        encoder.close()
                    encoder = AnnexBEncoder(
                        frame.width,
                        frame.height,
                        self.state.relay_bitrate,
                        fps=int(min(
                            max(1, int(getattr(source, "fps", 30) or 30)),
                            RELAY_MAX_FPS,
                        )),
                    )
                    announced = False
                if (
                    last_sent_at
                    and not self.state.relay_keyframe_wanted
                    and (loop.time() - last_sent_at) < (1.0 / allowed_fps)
                ):
                    continue

                force = self.state.relay_keyframe_wanted
                if force:
                    self.state.relay_keyframe_wanted = False
                    logger.info("Viewer asked for a keyframe; sending one")
                packets = await loop.run_in_executor(pool, encoder.encode, frame, force)
                if not announced:
                    if not packets:
                        continue
                    announced = True
                    logger.info(
                        "Relay stream %dx%d %s",
                        frame.width, frame.height, encoder.codec_string,
                    )
                    await ws.send_bytes(crypto.seal(TAG_CONFIG, json.dumps({
                        "codec": encoder.codec_string,
                        "width": frame.width,
                        "height": frame.height,
                    }).encode()))
                for is_key, timestamp, data in packets:
                    header = bytes([1 if is_key else 0]) + timestamp.to_bytes(8, "big")
                    before = loop.time()
                    await ws.send_bytes(crypto.seal(TAG_VIDEO, header + data))
                    stalled = loop.time() - before
                    blocked += stalled
                    last_sent_at = loop.time()
                    if stalled > RELAY_STALL_SECONDS and allowed_fps > RELAY_MIN_FPS:
                        allowed_fps = max(RELAY_MIN_FPS, allowed_fps * 0.5)
                        logger.info(
                            "Relay dropping to %.0f fps (one frame took %.0f ms "
                            "to hand over)", allowed_fps, stalled * 1000,
                        )
                        blocked = 0.0
                        window_start = loop.time()

                now = loop.time()
                span = now - window_start
                if span >= RELAY_ADAPT_WINDOW:
                    share = blocked / span
                    previous = allowed_fps
                    if share > RELAY_BLOCKED_HIGH:
                        allowed_fps = max(RELAY_MIN_FPS, allowed_fps * 0.6)
                    elif share < RELAY_BLOCKED_LOW:
                        allowed_fps = min(RELAY_MAX_FPS, allowed_fps * 1.5)
                    if abs(allowed_fps - previous) > 0.5:
                        logger.info(
                            "Relay %s to %.0f fps (%.0f%% of the last second "
                            "was spent waiting on the network)",
                            "slowing" if allowed_fps < previous else "speeding up",
                            allowed_fps, share * 100,
                        )
                    blocked = 0.0
                    window_start = now
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Relay video pump stopped")
        finally:
            pool.shutdown(wait=True)
            if encoder is not None:
                encoder.close()


    async def _negotiate(self, offer: RTCSessionDescription) -> dict:
        configuration = RTCConfiguration(
            iceServers=[RTCIceServer(urls=url) for url in self.state.ice_servers]
        )
        pc = RTCPeerConnection(configuration=configuration)
        self.state.peers.add(pc)

        @pc.on("connectionstatechange")
        async def on_connectionstatechange() -> None:
            state = pc.connectionState
            logger.info("Peer connection: %s", state)
            if self.state.on_state_change is not None:
                self.state.on_state_change(state)
            if state in ("failed", "closed"):
                await self._discard(pc)

        @pc.on("datachannel")
        def on_datachannel(channel) -> None:
            if channel.label == "audio":
                async def sink(payload: bytes) -> None:
                    if channel.bufferedAmount > AUDIO_CHANNEL_BACKLOG:
                        return
                    channel.send(payload)

                self._audio_sinks.add(sink)

                @channel.on("close")
                def on_audio_close() -> None:
                    self._audio_sinks.discard(sink)

                return
            logger.debug("Input channel open: %s", channel.label)

            def reply(payload: str) -> None:
                with contextlib.suppress(Exception):
                    channel.send(payload)

            @channel.on("message")
            def on_message(message) -> None:
                self._handle_input(message, reply)

            @channel.on("close")
            def on_close() -> None:
                self.state.controller.release_all()

        if "m=video" not in offer.sdp:
            await self._discard(pc)
            raise web.HTTPBadRequest(
                reason="Offer contained no video media; the viewer must request one"
            )

        track = ScreenTrack(self.state.source)
        transceiver = pc.addTransceiver(track, direction="sendonly")
        prefer_h264(transceiver)

        await pc.setRemoteDescription(offer)
        await pc.setLocalDescription(await pc.createAnswer())
        self._viewer_joined()
        return {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
        }

    async def _clipboard_copy(self, reply: Callable[[str], None] | None) -> None:
        """Copy on the Mac, then hand the result back to the viewer."""
        await self.state.controller.chord(["MetaLeft", "KeyC"])
        await asyncio.sleep(CLIPBOARD_SETTLE_SECONDS)
        text = clipboard.read_text()
        if reply is not None:
            reply(json.dumps({"t": "cb", "v": text or ""}))

    async def _clipboard_paste(self, text: str) -> None:
        """Put the viewer's clipboard on the Mac, then paste it."""
        if clipboard.write_text(text):
            await self.state.controller.chord(["MetaLeft", "KeyV"])

    def _handle_input(
        self, message: object, reply: Callable[[str], None] | None = None
    ) -> None:
        if not isinstance(message, str):
            return
        try:
            event = json.loads(message)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return

        kind = event.get("t")
        controller = self.state.controller
        try:
            if kind == "m":
                controller.mouse(
                    event.get("x", 0.0),
                    event.get("y", 0.0),
                    str(event.get("a", "move")),
                    str(event.get("b", "left")),
                )
            elif kind == "s":
                controller.scroll(
                    float(event.get("dx", 0.0)),
                    float(event.get("dy", 0.0)),
                    str(event.get("p", "move")),
                )
            elif kind == "k":
                controller.key(str(event.get("c", "")), bool(event.get("d", False)))
            elif kind == "x":
                controller.text(str(event.get("v", "")))
            elif kind == "c":
                keys = event.get("k")
                if isinstance(keys, list) and keys:
                    codes = [str(k) for k in keys][:6]
                    runner = (
                        controller.hold_chord if event.get("h") else controller.chord
                    )
                    asyncio.create_task(runner(codes), name="chord")
            elif kind == "g":
                asyncio.create_task(
                    controller.gesture(str(event.get("g", ""))), name="gesture"
                )
            elif kind == "q":
                self.apply_profile(str(event.get("p", "")))
            elif kind == "v":
                self._apply_viewport(int(event.get("w", 0)), int(event.get("h", 0)))
                self._viewport_seen.set()
            elif kind == "err":
                detail = str(event.get("m", ""))[:200]
                logger.warning("Viewer reports: %s", detail)
                print(f"\n  ! The viewer could not play the video: {detail}\n",
                      file=sys.stderr, flush=True)
            elif kind == "pg":
                if reply is not None:
                    reply(json.dumps({"t": "pg", "i": event.get("i")}))
            elif kind == "au":
                asyncio.create_task(
                    self._set_audio(bool(event.get("on")), reply), name="audio-toggle"
                )
            elif kind == "kf":
                self.state.relay_keyframe_wanted = True
            elif kind == "pv":
                hidden = not bool(event.get("on", True))
                if hidden != self.state.viewer_hidden:
                    self.state.viewer_hidden = hidden
                    logger.info(
                        "Viewer %s; %s sending video",
                        "hidden" if hidden else "visible",
                        "pausing" if hidden else "resuming",
                    )
                    if not hidden:
                        self.state.relay_keyframe_wanted = True
            elif kind == "cb":
                if not self.state.controller.enabled:
                    return
                if event.get("a") == "paste":
                    value = str(event.get("v", ""))
                    if value:
                        asyncio.create_task(
                            self._clipboard_paste(value), name="paste"
                        )
                else:
                    asyncio.create_task(self._clipboard_copy(reply), name="copy")
        except Exception:
            logger.exception("Ignoring malformed input event")

    async def _evict_peers(self) -> None:
        """Hang up on whoever held the session before this connection."""
        peers, self.state.peers = list(self.state.peers), set()
        if not peers:
            return
        logger.warning(
            "Another device connected with the session code; dropping %d "
            "existing viewer(s)",
            len(peers),
        )
        self._evicting = True
        try:
            for _ in peers:
                self._viewer_left()
            self.state.controller.release_all()
            await asyncio.gather(
                *(p.close() for p in peers), return_exceptions=True
            )
        finally:
            self._evicting = False

    async def _discard(self, pc: RTCPeerConnection) -> None:
        if pc in self.state.peers:
            self.state.peers.discard(pc)
            self._viewer_left()
            with contextlib.suppress(Exception):
                await pc.close()


    async def _on_shutdown(self, _app: web.Application) -> None:
        await self.close()

    async def close(self) -> None:
        self._audio_sinks.clear()
        async with self._audio_lock:
            await self._stop_audio()
        peers, self.state.peers = list(self.state.peers), set()
        for _ in peers:
            self._viewer_left()
        self.state.controller.release_all()
        if peers:
            await asyncio.gather(*(p.close() for p in peers), return_exceptions=True)


async def start_server(
    server: SignalingServer, port: int, host: str = "127.0.0.1"
) -> web.AppRunner:
    runner = web.AppRunner(server.app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    logger.debug("Signalling server listening on %s:%d", host, port)
    return runner


def build_controller() -> InputController:
    geometry = display_geometry()
    return InputController(geometry.width, geometry.height)
