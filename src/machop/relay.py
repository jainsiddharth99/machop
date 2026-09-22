"""Encrypted relay transport - the fallback when peer-to-peer ICE fails."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import struct
import time
from dataclasses import dataclass

import av
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger(__name__)

TAG_CONFIG = 0
TAG_VIDEO = 1
TAG_INPUT = 2
TAG_READY = 3
TAG_AUDIO = 4

NONCE_BYTES = 12
COUNTER_BYTES = 8
SALT_PREFIX_BYTES = NONCE_BYTES - COUNTER_BYTES


class RelayProtocolError(Exception):
    """The peer sent something that does not fit the protocol."""


def _hkdf(shared: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=length, salt=salt, info=info
    ).derive(shared)


@dataclass
class _Direction:
    """One key and one never-reused nonce sequence."""

    key: AESGCM
    prefix: bytes
    counter: int = 0

    def next_nonce(self) -> bytes:
        nonce = self.prefix + struct.pack(">Q", self.counter)
        self.counter += 1
        return nonce

    def nonce_for(self, counter: int) -> bytes:
        return self.prefix + struct.pack(">Q", counter)


class RelaySession:
    """Key agreement and record encryption for one relay connection."""

    def __init__(self, pin: str, *, role: str = "server", salt: bytes | None = None) -> None:
        if role not in ("server", "client"):
            raise ValueError("role must be 'server' or 'client'")
        self.role = role
        self._pin = pin.encode()
        self._private = ec.generate_private_key(ec.SECP256R1())
        self.salt = salt if salt is not None else os.urandom(16)
        self._send: _Direction | None = None
        self._receive: _Direction | None = None

    @property
    def public_key_bytes(self) -> bytes:
        return self._private.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )

    def establish(self, peer_public: bytes) -> bytes:
        """Derive both directions. Returns the confirmation tag to send back."""
        try:
            peer = ec.EllipticCurvePublicKey.from_encoded_point(
                ec.SECP256R1(), peer_public
            )
        except ValueError as exc:
            raise RelayProtocolError(f"Invalid peer key: {exc}") from exc

        shared = self._private.exchange(ec.ECDH(), peer)

        client_public = peer_public if self.role == "server" else self.public_key_bytes
        server_public = self.public_key_bytes if self.role == "server" else peer_public
        transcript = client_public + server_public + self.salt

        secret = shared + self._pin
        s2c = AESGCM(_hkdf(secret, self.salt, b"machop s2c"))
        c2s = AESGCM(_hkdf(secret, self.salt, b"machop c2s"))
        s2c_prefix = _hkdf(secret, self.salt, b"machop nonce s2c", SALT_PREFIX_BYTES)
        c2s_prefix = _hkdf(secret, self.salt, b"machop nonce c2s", SALT_PREFIX_BYTES)

        if self.role == "server":
            self._send = _Direction(s2c, s2c_prefix)
            self._receive = _Direction(c2s, c2s_prefix)
        else:
            self._send = _Direction(c2s, c2s_prefix)
            self._receive = _Direction(s2c, s2c_prefix)

        confirm_key = _hkdf(secret, self.salt, b"machop confirm")
        return hmac.new(confirm_key, transcript, "sha256").digest()

    def seal(self, tag: int, payload: bytes) -> bytes:
        """Encrypt one record: [8B counter][ciphertext]."""
        if self._send is None:
            raise RelayProtocolError("Key agreement has not completed")
        counter = self._send.counter
        nonce = self._send.next_nonce()
        blob = self._send.key.encrypt(nonce, bytes([tag]) + payload, None)
        return struct.pack(">Q", counter) + blob

    def open(self, record: bytes) -> tuple[int, bytes]:
        if self._receive is None:
            raise RelayProtocolError("Key agreement has not completed")
        if len(record) < COUNTER_BYTES + 1:
            raise RelayProtocolError("Truncated record")
        (counter,) = struct.unpack(">Q", record[:COUNTER_BYTES])
        if counter < self._receive.counter:
            raise RelayProtocolError("Replayed or reordered record")
        self._receive.counter = counter + 1
        plaintext = self._receive.key.decrypt(
            self._receive.nonce_for(counter), record[COUNTER_BYTES:], None
        )
        if not plaintext:
            raise RelayProtocolError("Empty record")
        return plaintext[0], plaintext[1:]


DEFAULT_CODEC_STRING = "avc1.42C02A"


def codec_string_from_annexb(data: bytes) -> str | None:
    """Read the real profile, constraints and level out of the SPS."""
    index = 0
    limit = len(data) - 4
    while index < limit:
        if data[index : index + 3] == b"\x00\x00\x01":
            start = index + 3
        elif data[index : index + 4] == b"\x00\x00\x00\x01":
            start = index + 4
        else:
            index += 1
            continue
        if data[start] & 0x1F == 7 and start + 4 <= len(data):
            profile, constraints, level = data[start + 1 : start + 4]
            return f"avc1.{profile:02X}{constraints:02X}{level:02X}"
        index = start + 1
    return None


class AnnexBEncoder:
    """H.264 in Annex-B, which is what WebCodecs accepts without a description."""

    def __init__(
        self,
        width: int,
        height: int,
        bitrate: int,
        fps: int = 30,
        pix_fmt: str = "nv12",
    ) -> None:
        self.width = width
        self.height = height
        self.fps = fps
        self.pix_fmt = pix_fmt
        self._context = av.CodecContext.create("libx264", "w")
        self._context.width = width
        self._context.height = height
        self._context.pix_fmt = pix_fmt
        self._context.bit_rate = bitrate
        self._context.framerate = __import__("fractions").Fraction(fps, 1)
        self._context.time_base = __import__("fractions").Fraction(1, fps)
        self._context.options = {
            "preset": "ultrafast",
            "tune": "zerolatency",
            "profile": "baseline",
            "level": "42",
            "x264-params": (
                f"vbv-maxrate={bitrate // 1000}:"
                f"vbv-bufsize={max(bitrate // 1000 // fps * 2, 64)}:"
                "annexb=1:repeat-headers=1:sliced-threads=0:threads=1"
            ),
        }
        self._started = time.monotonic()
        self._count = 0
        self.codec_string = DEFAULT_CODEC_STRING

    def encode(
        self, frame: av.VideoFrame, force_keyframe: bool = False
    ) -> list[tuple[bool, int, bytes]]:
        """Returns (is_keyframe, timestamp_microseconds, annexb_bytes)."""
        if frame.format.name != self.pix_fmt:
            frame = frame.reformat(format=self.pix_fmt)
        elapsed = time.monotonic() - self._started
        frame.pts = self._count
        self._count += 1
        frame.time_base = self._context.time_base
        frame.pict_type = (
            av.video.frame.PictureType.I
            if force_keyframe
            else av.video.frame.PictureType.NONE
        )

        out: list[tuple[bool, int, bytes]] = []
        for packet in self._context.encode(frame):
            data = bytes(packet)
            if packet.is_keyframe:
                found = codec_string_from_annexb(data)
                if found:
                    self.codec_string = found
            out.append((bool(packet.is_keyframe), int(elapsed * 1_000_000), data))
        return out

    def close(self) -> None:
        try:
            self._context.close()
        except Exception:
            logger.debug("Encoder close failed", exc_info=True)
