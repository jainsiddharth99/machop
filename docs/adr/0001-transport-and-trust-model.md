# ADR-0001: Transport, capture pipeline and trust model for Machop

**Status:** Accepted
**Date:** 2026-09-18
**Deciders:** Siddharth Jain

## Context

Machop streams a Mac's screen to a phone/tablet/other Mac on a *different*
network and sends input back. The v0.1 implementation did not work at all:
undeclared `numpy`, a fatal reshape in every captured frame, and an SDP
negotiation that returned HTTP 500 because the browser offered no `m=video`
line. Beyond the blockers, three forces shape the design:

1. **Latency is the product.** A screen mirror you interact with is unusable
   above ~150 ms round trip. v0.1 measured **0.4 fps** because it encoded a
   native-Retina 2940x1912 frame with libvpx VP8 (2405 ms/frame).
2. **It must connect from anywhere with zero setup.** The user runs one command
   on the Mac and opens a URL on the phone. No TURN signup, no VPN, no
   `cloudflared` install, no port forwarding, no account.
3. **The screen is the most sensitive possible payload.** The tunnel provider
   (`localhost.run`) **terminates TLS**. In v0.1 it could watch the entire
   screen in plaintext and read the PIN. Any acceptable design must reduce the
   relay to a pipe carrying bytes it cannot interpret.

### Measured facts that drove the decision

All numbers from this Mac (Apple Silicon, macOS 27.0), real screen content:

| Encoder @1470x956 | ms/frame | fps ceiling | bitrate |
|---|---|---|---|
| libvpx VP8 @2940x1912 *(v0.1)* | 2405 | **0.4** | - |
| h264_videotoolbox (hardware) | 2.74 | 365 | 4.8 Mbps |
| **libx264 ultrafast/zerolatency** | **1.35** | **741** | **3.6 Mbps** |

| Capture path | ms/frame | notes |
|---|---|---|
| CGDisplayCreateImage + BGRA->YUV *(v0.1)* | ~45 | blocking, deprecated, full-res |
| ScreenCaptureKit, hardware-scaled NV12 | ~1 | callback-driven, non-deprecated |

Two conclusions, both counter to the obvious guess:

- **Software x264 beats the hardware encoder here** on both speed and bitrate
  efficiency. VideoToolbox plumbing is unnecessary complexity - deleted from
  the design. The v0.1 disaster was VP8-at-Retina, not software encoding.
- **Capture, not encode, is the real CPU cost.** ScreenCaptureKit removes it.

## Decision

Five decisions, recorded together because they interlock.

### D1. Media rides WebRTC (DTLS-SRTP), with an encrypted relay fallback

WebRTC is primary. Media is encrypted peer-to-peer and **never traverses the
relay**; the tunnel carries only signalling. When ICE fails (symmetric NAT on
both ends - carrier CGNAT is the common case) we fall back to an
AES-GCM-encrypted WebSocket over the same tunnel, decoded with WebCodecs.

### D2. ScreenCaptureKit for capture, hardware-scaled at source

`SCStream` with `SCStreamConfiguration.width/height` set to the *target* size,
so the downscale happens in hardware and we never touch a 2940x1912 buffer.
`CGDisplayCreateImage` is kept only as a fallback for pre-13.0 systems.

### D3. Force H.264; never negotiate VP8

aiortc advertises VP8 first, which is how v0.1 would have landed on the 0.4 fps
path even after the SDP bug was fixed. We call `setCodecPreferences` to pin
H.264 and raise aiortc's 3 Mbps ceiling, which is too low for legible text.

### D4. Zero infrastructure - `ssh` and nothing else

`ssh` ships on every Mac. No TURN service, no Cloudflare account, no VPS. The
cost is accepting a free relay, which D5 makes safe enough to accept.

### D5. Single-use, expiring, session-bound PIN

The user asked to keep a typed PIN. A PIN typed into a relay-served page
crosses that relay in plaintext, so we make the plaintext worthless instead of
trying to hide it: the PIN authenticates **exactly one** session, expires
unused after 5 minutes, is burned on first successful pair, and is compared
with `hmac.compare_digest` behind exponential-backoff rate limiting.

## Options Considered

### Option A: Relay everything over the tunnel (WebSocket + WebCodecs)

| Dimension | Assessment |
|---|---|
| Complexity | Low - one transport, no ICE/STUN/SDP, drops aiortc |
| Connects | 100% of networks, unconditionally |
| Latency | ~40-120 ms (relay hop, TCP head-of-line blocking) |
| Relay exposure | Ciphertext only, if we add AES-GCM ourselves |

**Pros:** simplest possible thing that always works; we control the wire format,
so dirty-rect and frame pacing are available; no NAT failure modes to support.
**Cons:** every byte crosses a third party and counts against their bandwidth;
TCP retransmits on a lossy cellular link cause visible stalls, with no
congestion control tuned for real-time media.

### Option B: WebRTC P2P, encrypted relay as fallback *(chosen)*

| Dimension | Assessment |
|---|---|
| Complexity | High - two transports, two codec paths |
| Connects | 100% (P2P ~80%, fallback covers the rest) |
| Latency | ~20-40 ms direct, ~40-120 ms fallback |
| Relay exposure | Nothing at all on the P2P path; ciphertext on fallback |

**Pros:** lowest achievable latency on the common path; UDP with real congestion
control degrades gracefully on cellular instead of stalling; on the P2P path the
relay sees *only* signalling, which is the strongest possible answer to the
TLS-termination problem.
**Cons:** roughly double the code and test surface; ICE failures are notoriously
hard to diagnose; needs a working fallback anyway, so Option A's code is a
subset of this one's.

### Option C: WebRTC P2P only, with TURN

| Dimension | Assessment |
|---|---|
| Complexity | Medium |
| Connects | ~85% on STUN alone; ~100% only with TURN configured |
| Cost | TURN service signup or self-hosted coturn |

**Pros:** single transport, lowest latency.
**Cons:** directly contradicts requirement 2 - every user must obtain TURN
credentials before the tool works on cellular. Rejected on that ground alone.

## Trade-off Analysis

The decisive tension is **Option A's simplicity vs Option B's latency and
relay-exposure story.**

Option A is genuinely tempting: it is roughly half the code, it cannot fail to
connect, and WebCodecs with `optimizeForLatency` avoids the jitter buffer that
makes naive WebRTC `<video>` playback feel sluggish - so its latency disadvantage
is smaller than the raw numbers suggest.

Option B wins on two grounds that matter more. First, on the ~80% of networks
where P2P succeeds, **the relay carries no media at all** - not ciphertext,
nothing. Given that the relay terminates TLS and is the single largest
unmitigated risk in the system, removing it from the media path entirely is a
qualitatively better answer than encrypting past it. Second, screen mirroring
over cellular is exactly the workload where TCP head-of-line blocking hurts:
one lost packet stalls every subsequent frame, whereas SRTP over UDP drops it
and moves on.

The cost is real and should be stated plainly: this is about twice the code, and
the fallback path must be built and tested regardless, so we pay for Option A
*and* the WebRTC stack. We accept that because connect-reliability and
relay-exposure are non-negotiable and latency is the product.

**On D5 (PIN):** a PIN typed into a page the relay serves cannot be hidden from
that relay by any in-browser cryptography - a malicious relay can simply replace
`app.js`. A PAKE (SPAKE2/EKE) would defeat a *passive* relay that logs traffic,
but not an active one. Single-use + expiry + session-binding achieves most of
the same practical benefit against the passive case at a fraction of the
complexity, so we do that and document the residual limit rather than shipping
a bespoke PAKE that invites false confidence.

## Consequences

**What becomes easier**
- Interaction becomes usable: ~1 ms encode and ~1 ms capture replace ~2450 ms.
- Onboarding stays one command; nothing to sign up for or install.
- On the P2P path the tunnel provider is cryptographically irrelevant.
- H.264 is hardware-decoded on every iPhone/iPad, so the phone stays cool.

**What becomes harder**
- Two transports must be kept correct; an integration test must exercise both.
- ICE failure diagnosis needs explicit surfacing (connection state -> clear
  terminal message), or users see an unexplained black screen.
- ScreenCaptureKit is callback-driven on a dispatch queue, so frames cross a
  thread boundary into asyncio - needs a bounded, latest-frame-wins queue.

**What we will need to revisit**
- If P2P success rate measures below ~70% in practice, revisit optional TURN.
- If a malicious relay moves from theoretical to real, the answer is a native
  client or a self-hosted tunnel, not more in-browser crypto.
- WebCodecs needs Safari 16.4+ (iOS 16.4, 2023). Older iOS gets the WebRTC path
  only; if that matters, the fallback needs an MSE variant.

## Action Items

All complete as of 2026-09-18; see the commit history and `tests/`.

1. [x] Consolidate three packages into one `machop` package.
2. [x] Declare `numpy`/`av`; pin scoped pyobjc frameworks, not the metapackage.
3. [x] ScreenCaptureKit frame source, hardware-scaled, Quartz fallback.
4. [x] Fix SDP: `addTransceiver('video', recvonly)`; pin H.264; raise bitrate.
5. [x] Full input set: move/drag/scroll/right/double-click, modifiers, unicode.
6. [x] Single-use expiring PIN, backoff rate limiter, bind 127.0.0.1.
7. [x] Encrypted WebSocket + WebCodecs fallback on ICE failure.
8. [x] Clean shutdown: close peers, reap ssh, no orphan tunnels.
9. [x] Integration tests that negotiate a real session and capture a real frame.


---

## Amendment, 2026-09-20: tunnel backends

**D4 is narrowed.** The original decision kept `ssh`/localhost.run as the only
transport and listed Tailscale Funnel as an opt-in for a permanent URL. Two
things changed after measurement:

1. **cloudflared is ~12x faster on signalling** - ~0.10 s per request against
   localhost.run's ~1.21 s. The default is now `--tunnel auto`: cloudflared
   when installed, localhost.run otherwise. Zero-setup survives for anyone
   without it, so requirement 2 is intact.
2. **The Tailscale backend is removed.** It was never verified end to end, it
   requires an account, and its `100.64.0.0/10` address range overlaps what
   NetBird - the operator's corporate VPN - already occupies at `100.88/16`.
   Its DNS management in particular would contend with NetBird's resolver.
   The permanent URL it offered is adequately replaced by `--notify`, which
   pushes a changed URL to the user's phone.

**Consequence:** no backend now offers a stable URL, so `--notify` moves from
a convenience to the recommended way to run an unattended session. If a stable
URL is ever needed, revisit Cloudflare *named* tunnels (permanent hostname,
but requires a Cloudflare account and a domain) rather than reinstating
Tailscale.

Two findings recorded so they are not rediscovered:

- cloudflared prints its URL roughly 10 s before it resolves, and macOS caches
  the resulting NXDOMAIN, so an early visit keeps failing afterwards. The
  backend now waits for edge registration and polls until the URL answers.
- Two concurrent machop instances cannot both hold an SCStream; the second
  degrades to the Quartz path.

## Amendment, 2026-09-21: resuming a session without the code

D5 said a PIN authenticates exactly one session and is burned on first use.
That still holds, but the *device* that burned it can now come back without
retyping it: `/offer` accepts a request carrying the session cookie and no
PIN, and re-claims the session for that cookie.

**Why.** The tool exists so you can start a long job and leave. A phone
moving from wifi to cellular gets a new address, ICE fails, and the viewer
fell back to a keypad - in a pocket, unattended, several hours into a
deployment. In practice that meant the session was lost exactly when it was
most wanted.

**Why this is not a weakening.** The cookie is 18 bytes from
`secrets.token_urlsafe` - 144 bits - set httpOnly and SameSite=Strict, and
Secure whenever the transport is TLS. It is a strictly stronger secret than
the six digits it stands in for, and unlike the PIN it is never typed, shown
on screen, or carried in a request body the tunnel operator can read. There
is nothing to brute force, so resume is deliberately not rate limited;
throttling it could only ever lock out the legitimate device.

**Ordering.** `authenticate()` moves `_last_claimed_by` to whoever most
recently proved they know the code. A second device that authenticates
therefore displaces the first, which must type the code again rather than
silently taking the session back.

**Known limit.** The relay fallback derives its encryption keys from the PIN
(D7), so a session resumed from the cookie alone cannot use it. The viewer
asks for the code if peer-to-peer fails on a resumed session. Closing that
would mean either storing the PIN in the browser, where script can read it,
or re-keying the relay off the cookie - neither worth it for a fallback path.

## Amendment, 2026-09-21: the code takes a session over

D5's single-session rule was enforced by refusing a second device with HTTP
409 while the first still held the claim. That refusal is removed: a device
presenting the correct code claims the session and the previous holder is
hung up on.

**Why.** The 409 did not achieve what it was for. Someone who had observed
the code only had to wait for the real viewer to disconnect, so it never
prevented a second viewer - it only delayed one. Meanwhile it reliably
locked out the legitimate owner, because the Mac cannot distinguish a
closed browser from a quiet one until ICE times out around thirty seconds
later. Closing a laptop and picking up a phone was refused for that whole
window, which is precisely the situation this tool exists to serve.

**What replaces it.** Eviction is announced, not silent: the Mac logs a
warning naming how many viewers were dropped, and the displaced viewer's
peer connection is closed rather than left running alongside the new one.
A second viewer therefore cannot arrive quietly, which is the property that
actually matters. A wrong code evicts nobody and is still throttled by the
existing backoff, so takeover is not a way to brute force without penalty.

**Ordering.** Eviction runs after authentication and before negotiation,
with `_evicting` set so that the teardown's `_viewer_left` does not release
the claim the newcomer has just taken.

## Amendment, 2026-09-21: the relay announces what it actually encodes

The relay's `TAG_CONFIG` used to declare a fixed `avc1.42E01E` - Baseline,
level 3.0 - whatever it was really sending. Measured, the stream was
Baseline **level 6.2**, because nothing constrained x264's choice.

Chrome decodes it anyway. Safari does not: WebCodecs configures a hardware
decoder from that string, and iOS hardware does not implement level 6.2 at
all. The result is no picture and no error - input keeps working, so it
reads as a slow network rather than a broken stream. It cost several rounds
of debugging precisely because every test on a Mac passed.

Two changes. The encoder pins `level=42`, which every iOS device decodes and
which has ample headroom for a Retina display at 60 fps. And the announced
codec string is parsed from the first keyframe's SPS, so the declaration can
no longer drift from the bitstream - a test asserts the two agree.

The peer-to-peer path is deliberately left alone. WebRTC decoders key off
the SPS rather than the SDP's `profile-level-id`, and that path is working;
changing it would be a guess with a regression attached.

### Addendum: one slice per frame

The same grey screen had a second cause, found only after the codec string
was corrected and an iPhone still showed a band of picture across the top
with grey beneath it.

`tune=zerolatency` enables x264's sliced-threads, which at 1290x838 splits
every frame into eight slices - eight NAL units inside one
`EncodedVideoChunk`. Chrome decodes all of them; Safari decodes the first
and discards the rest, which renders as roughly the top eighth of the
screen. One eighth of 838 rows is about 105, which is what "a little
distorted top part" turned out to mean.

The fix is `threads=1`, not merely `sliced-threads=0`. Slices are precisely
how zerolatency avoids frame-threading latency, so removing them without
also removing the threads hands the work back to frame threading and the
encoder buffers - measured as `encode()` returning no packets at all. Both
properties are now asserted: exactly one slice per frame, and a packet out
for every frame in.

Encoding single-threaded costs about 2 ms per frame at these resolutions,
against a network path measured in tens of milliseconds.

### Addendum: the relay's frame rate

The grey screen had one more cause, and it was the largest: the relay
encoder never told x264 what frame rate it was encoding at.

x264 takes its frames-per-second from the codec context's time base. The
encoder set `time_base = 1/90000`, which is the RTP media clock and the
right answer for the peer-to-peer path, but here it told x264 to expect
90,000 frames a second. Rate control then spread 4 Mbps across them -
about forty bits per frame - and every picture decoded as a flat grey
wash. Measured 7.6 dB PSNR, against 48 dB with `framerate` set and a
matching time base.

This was not a Safari problem, though it was diagnosed as one twice. It
was equally broken in Chrome; Chrome simply rendered the grey wash without
complaint while the tests counted frames and reported success.

The lesson is in the test suite rather than the code. Every relay test
checked structure - NAL unit types, one slice per frame, the codec string
matching the SPS, packets arriving promptly - and every one passed
throughout. None compared a decoded pixel against its source, so a
completely blank stream satisfied all of them. `test_the_relay_encoder_
actually_preserves_the_picture` closes that gap, and asserts PSNR rather
than structure.

## Amendment, 2026-09-21: sharing the Mac's sound

**Decision.** Sound is captured by a **second, independent** ScreenCaptureKit
stream, encoded as Opus, and carried on whichever transport is live: an
unreliable, unordered data channel on the peer-to-peer path, and a
`TAG_AUDIO` record on the encrypted relay. It is off until a viewer presses
the speaker button, and stops when the viewer goes.

**Why a separate capture stream.** Changing capture resolution tears the
video stream down and builds a new one, which the viewer does as soon as it
reports its viewport. Sharing one stream would put a hole in the sound every
time the picture resized. Two streams cost a little CPU; a dropout is far
more noticeable.

**Why the layout was verified rather than assumed.** ScreenCaptureKit hands
over **non-interleaved** float32, and libopus wants interleaved. Reading that
backwards does not crash - it produces exactly the right number of packets of
exactly the right size, and sounds like noise. It was confirmed against the
hardware by playing a tone with signal in one channel only and checking which
half of the buffer carried it; the `kAudioFormatFlagIsNonInterleaved` bit is
still checked at runtime.

**Why unreliable for the peer-to-peer channel.** A retransmitted audio packet
arrives after the moment it was meant to be heard *and* blocks whatever is
queued behind it. Reliable ordered delivery would trade one click for a
growing lag. Input keeps the reliable channel; sound gets its own.

**Why `lowdelay`.** Opus's default application adds 26.5 ms of algorithmic
delay. `lowdelay` drops that to about 5 ms by skipping the SILK layer, which
at 96 kbps is not audible on screen audio - and twenty milliseconds of
lip-sync is worth more here than the last of the codec's efficiency.

**Why constrained VBR.** Plain `vbr=on` overshot the ceiling by a third on
real content: 126 kbps measured against a 96 kbps target. On a metered tunnel
the ceiling is the number that matters.

**Why silence is not sent.** A silent Opus frame is three bytes, but the
record framing and AES tag around it are not, and fifty encrypted records a
second is not nothing. After half a second of digital silence the Mac stops
until something plays.

**Backpressure is the capture queue, not a timeout.** The audio pump simply
waits on the send. While it waits nothing reads the capture queue, which is
bounded and drops its oldest blocks - the right place for it, because a whole
20 ms frame goes at once rather than half of one. A timeout around the send
was written first and removed: cancelling a WebSocket write part-way through
is a far worse failure than a late packet, on a connection that also carries
the video.

### Addendum: `resume()` that never settles

Written against real Safari 27, which is the same WebKit as iOS 27.

`audioEnable()` originally did `await audioCtx.resume()`. With no user
activation, Safari returns a promise that **neither resolves nor rejects**.
The function hung forever, so the button did nothing at all and the Mac was
never even asked for sound - a dead control with no error anywhere.

The fix is to never depend on that answer. `resume()` is called and its
result ignored; the context works perfectly well suspended, the worklet and
decoder are built anyway, and it is nudged again as packets arrive. If it is
still not running after a couple of seconds of audio, the viewer says so.

This is the same failure shape as the grey screen: a path where nothing
throws, nothing logs, and the feature is simply absent. `playDecoded` is now
wrapped so that anything failing inside the decoder callback is reported to
the user and to the Mac's log rather than silently stopping the sound.

## Amendment, 2026-09-21: direct first, briefly, then relay

**Decision.** The viewer gives a direct connection 1.6 seconds, not eight,
and `--prefer relay` prints a URL that skips the attempt.

Eight seconds was chosen to be generous to slow NAT traversal. In practice it
was eight seconds of grey screen on every cellular connection, because
carrier-grade NAT makes the attempt hopeless rather than slow. On a LAN the
host candidates match in well under half a second, so a short deadline
separates the two cases cleanly: same network goes direct, everything else
relays almost immediately.

`--prefer relay` is a **query parameter on the printed address**, not server
state. The same running Mac therefore stays reachable both ways - the printed
URL skips straight to the relay from a train, the plain URL still goes direct
from the sofa.

### Addendum: what the latency work actually bought

Measured on this machine, 1440x928 screen content:

| change | before | after |
|---|---|---|
| peak frame size (VBV) | 247 KB | 13 KB |
| relay while scrolling | 66 fps, 1.24 Mbps | 22 fps, 0.72 Mbps |
| input wait behind the encoder | 9.21 ms median | 0.22 ms median |
| encode, NV12 instead of yuv420p | 3.45 ms/frame | 3.18 ms/frame |
| viewer decode queue | 4 frames (~133 ms) | 2 frames (~66 ms) |

The input figure is the one that is easy to miss. Encoding in a worker thread
does not make encoding faster - wall time was 3.52 ms/frame before and
3.46 ms after - it stops pointer and scroll events waiting behind it, because
they arrive on the same event loop.

## Implementation hazards

Every item here cost debugging time, and every one of them fails *silently* -
no exception, no log line, just a feature that is quietly absent or wrong.
They are recorded here rather than in the source.

### PyObjC and ScreenCaptureKit

- **Completion handlers must return `None`.** An exception escaping into the
  ObjC runtime calls `abort()` and takes the whole process with it. Every
  handler wraps its body and swallows.
- **The `SCStreamOutput` delegate must be held by a strong reference.** Let it
  be garbage collected and frames stop arriving with no error at all.
- **The delegate ObjC class must be registered exactly once.** Declaring it
  inside `start()` re-registers the same class name on a second call, which the
  runtime rejects - capture then silently fell back to the Quartz path for
  every session after the first.
- **`loop.call_soon_threadsafe` can raise after the loop closes.** A capture
  callback firing during interpreter shutdown raised "Event loop is closed"
  straight into ObjC, which aborts. `is_closed()` is not enough; the call is
  wrapped.
- **`CGDataProviderCopyData` over-reports length.** The buffer carries trailing
  surface alignment padding beyond `height * stride`; trim before reshaping.
- **System audio is non-interleaved float32.** Verified against the hardware by
  playing a tone in one channel only. Reading it as interleaved does not crash;
  it sounds like noise.

### x264

- **`framerate` and `time_base` must both be set, and must agree.** x264 takes
  its frames-per-second from the time base. A `1/90000` clock - correct for
  RTP, wrong for a codec context - told x264 to expect 90,000 fps, and it split
  the bitrate across them at about forty bits per frame. Measured 7.6 dB PSNR.
- **`threads=1`, not just `sliced-threads=0`.** `tune=zerolatency` enables
  sliced threads, which splits each frame into eight slices; Safari decodes the
  first and discards the rest. But turning slices off alone hands the work to
  x264's *frame* threading, which buffers several frames before emitting any -
  measured as `encode()` returning nothing at all. One slice per frame means
  one thread.
- **`bit_rate` is an average, not a ceiling.** Without VBV, x264 spent 247 KB
  on a frame whose mean was 2.3 KB. VBV bounded to two frames of buffer brings
  the peak to 13 KB; the buffer size *is* the worst-case added latency.
- **Pin the level.** Unconstrained, x264 labels the stream 6.2, which no
  iPhone's hardware decoder accepts. 4.2 is universally supported.
- **Feed it NV12.** It is what ScreenCaptureKit produces and what libx264
  accepts; asking the context for `yuv420p` inserts a whole-frame colour
  conversion on every frame.
- **Encode off the event loop.** It costs ~3.2 ms per frame, during which
  input arriving on the same socket cannot be processed.

### WebRTC

- **Add the transceiver before `setRemoteDescription`.** That is where aiortc
  filters codecs against our preferences; pinning H.264 afterwards is silently
  ignored and VP8 wins, at 2405 ms/frame.
- **The offer must carry an `m=video` line.** Adding a track when it does not
  yields a transceiver with no offered direction and aiortc raises.
- **The server binds `127.0.0.1` only.** Never `0.0.0.0`; the tunnel connects
  from loopback. Regression-tested.

### The viewer

- **Size the canvas from the decoded frame, never from the config message.**
  `drawImage` draws at the frame's natural size, so a canvas sized ahead of the
  stream renders the top-left corner of a larger picture at 1:1 - fully
  painted, entirely the wrong scale.
- **The declared codec string must match the bitstream exactly.** Safari
  configures its hardware decoder from it and then fails quietly on anything
  that does not match. It is parsed from the SPS, never hardcoded.
- **A `VideoDecoder` error is terminal.** Its state goes to `closed` and every
  later frame is dropped, so an error handler must rebuild rather than report.
- **`getBoundingClientRect()` returns 0x0 before the stage is shown**, so the
  size in the relay handshake was silently dropped; there is a window
  fallback.
- **Safari's `AudioContext.resume()` never settles without user activation** -
  it neither resolves nor rejects. Awaiting it hangs.
- **Browser storage throws in private mode.** Every access is guarded.

### Lifecycle

- **Power assertions must be released on every exit path**, including
  exceptions and signals. A leaked assertion keeps the Mac awake indefinitely.
- **Per-viewer state must not outlive the viewer.** A phone that dropped while
  its tab was hidden left the stream paused for whoever connected next.
- **`str.lstrip()` strips characters, not a prefix.** `"https://shop.example"`
  lost its `s`, `h`, `o` and `p`.
