# Machop

Stream your Mac's screen to a phone, tablet or another computer over the
public internet, and control it from there. One command, no account, nothing
to install on the other device — it runs in the browser.

Built for checking on a long job from somewhere else: start a deploy, leave
the house, watch it finish from your phone and fix it if it breaks.

```bash
pip install machop
machop
```

It prints a URL and a six-digit code. Open the URL anywhere, type the code.

## What you get

- **Low latency.** H.264 over WebRTC peer-to-peer when both ends can reach
  each other, an encrypted WebSocket relay when they cannot. Typically well
  under 100 ms on a decent link.
- **Full control.** Pointer, keyboard, scroll, right click, Mission Control,
  Cmd-Tab, and copy/paste in both directions.
- **Sound**, on request — the 🔊 button shares whatever the Mac is playing.
- **Touch gestures** that match a trackpad: one finger taps and drags, two
  fingers scroll, two-finger tap right-clicks, three fingers switch spaces.
- **It survives being left alone.** The Mac will not sleep while someone is
  watching, the tunnel reconnects by itself, and the code stays valid until
  you stop the tool.

## Requirements

macOS 13 or later, Python 3.10+. Grant Screen Recording and Accessibility
when macOS asks — the tool checks and tells you what is missing.

## The toolbar

| | |
|---|---|
| `cmd` `tab` `⌘⇥` | modifier, key, and the app switcher held open |
| `esc` `^C` | escape, and interrupt |
| `copy` `paste` | clipboard, both directions |
| `1:1` | magnify, and re-ask the Mac for matching resolution |
| `🔊` | share the Mac's sound |
| `⌨` | soft keyboard |
| `⋯` | arrows, ctrl/opt/shift, quality, scroll speed, fullscreen |

## Options

| flag | default | |
|---|---|---|
| `--pin` | random 6 digits | fixed session code |
| `--profile` | `gui` | `text` is sharp and cheap, `gui` is smoother |
| `--max-width` | 1440 | capture width in pixels |
| `--bitrate` | per profile | video ceiling in Mbps |
| `--prefer` | `auto` | `relay` prints a URL that skips the direct attempt |
| `--tunnel` | `auto` | `localhost.run`, `cloudflared`, `ngrok`, `cloudflare-named` |
| `--no-audio` | off | never share sound, whatever the viewer asks |
| `--no-relay` | off | peer-to-peer only; screen data never transits a third party |
| `--view-only` | off | stream but ignore all input |
| `--no-tunnel` | off | serve on localhost only |

`machop --help` lists the rest.

For a permanent URL that does not change between runs, see
[docs/tunnels.md](docs/tunnels.md).

## Connecting

On the same WiFi the two ends find each other directly and nothing is
relayed. On mobile data they usually cannot: carrier-grade NAT gives the
phone a private address with no route back in, so there is nothing to
connect to. That is not a bandwidth problem — a fast 5G connection fails
where slow WiFi works.

So the viewer tries direct for about a second and a half, then switches to
the encrypted relay. Media on that path is encrypted end to end with
AES-256-GCM before it reaches the relay, using a key derived from the
session code.

## Security

The tunnel provider terminates TLS, so it can see what the browser posts.
The design assumes that rather than pretending otherwise:

- The server binds `127.0.0.1` only. The tunnel reaches it from loopback.
- The session code authenticates one device, and is rate limited.
- Another device with the code takes the session over, and the Mac says so.
- Relay media is encrypted end to end; the relay carries bytes it cannot read.

This does not protect against a relay that serves modified JavaScript. If
that is in your threat model, use `--no-relay`, or do not expose the machine
at all. The reasoning is written up in
[docs/adr/0001-transport-and-trust-model.md](docs/adr/0001-transport-and-trust-model.md).

## Development

```bash
pip install -e ".[dev]"
pytest
node --test tests/web/
```

The suite negotiates real WebRTC sessions against real screen capture, runs
a live encrypted relay, and checks decoded pixels rather than packet counts.
Tests that need a real display are skipped automatically on build machines,
so run the full suite on a Mac before opening a pull request.

## Licence

MIT. See [LICENSE](LICENSE).
