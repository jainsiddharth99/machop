# Machop

Stream your Mac's screen to a phone, tablet or another computer over the
public internet, and control it from there. One command, no account, nothing
to install on the other device — it runs in the browser.

Built for checking on a long job from somewhere else: start a deploy, leave
the house, watch it finish from your phone and fix it if it breaks.

<p align="center">
  <img src="https://raw.githubusercontent.com/jainsiddharth99/machop/main/docs/images/demo.gif"
       alt="Starting Machop on a Mac and connecting to it from an iPhone"
       width="560">
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/jainsiddharth99/machop/main/docs/images/hero.png"
       alt="A Mac's screen running on an iPhone, with the Machop toolbar along the bottom"
       width="760">
</p>

```bash
pipx install git+https://github.com/jainsiddharth99/machop.git
machop
```

Needs **Python 3.10 or newer** and macOS 13+. If you do not have `pipx`,
`brew install pipx`.

macOS will ask for **Screen Recording** and **Accessibility** the first time.
The first run is slow while macOS warms its framework cache; every run after
is under a second.

Update later with `pipx upgrade machop`.

It prints a URL and a six-digit code. Open the URL anywhere, type the code.

<p align="center">
  <img src="https://raw.githubusercontent.com/jainsiddharth99/machop/main/docs/images/terminal.png"
       alt="The terminal banner: address, code, capture size, sound and fallback"
       width="620">
</p>

## Get a permanent link

By default the address changes every run, which means re-reading a new URL off
your Mac every time — awkward when the whole point is to not be at it.

A free ngrok account fixes that. You get one reserved domain that is yours
permanently, so you can bookmark it on your phone once and never look at the
Mac again.

**One-time set-up, about two minutes:**

1. Sign up free at [dashboard.ngrok.com](https://dashboard.ngrok.com).
2. `brew install ngrok`
3. Copy your authtoken from **Getting Started → Your Authtoken** and run:
   ```bash
   ngrok config add-authtoken YOUR_TOKEN
   ```
4. Go to **Domains** and create one. ngrok assigns the name — you cannot pick
   it — and it looks like `three-random-words.ngrok-free.dev`.

**Then, every time:**

```console
$ machop --ngrok-domain your-domain.ngrok-free.dev
  Opening secure tunnel via ngrok…

  Machop is live

    Open      https://your-domain.ngrok-free.dev
    Code      731204

    Display   1470x956 pt  ->  streaming 1440x928
    Input     enabled
    Sound     on request (the speaker button in the viewer)
    Fallback  encrypted relay if a direct connection does not come up

    The code stays valid until you stop the tool, and survives a
    tunnel reconnect. Write it down before you leave.

  Press Ctrl-C to stop.
```

Same address forever. Bookmark it on your phone, add it to your home screen,
and connecting is one tap plus the code.

A reserved domain is also the fastest way to start: it already exists in DNS,
so the tunnel answers the moment it opens. The default quick tunnel has to
wait for a brand-new hostname to be published, which takes about ten seconds
every run.

You do not need to start ngrok separately — Machop launches and stops it for
you.

Two things worth knowing: the free plan includes 1 GB of transfer a month,
which is plenty for normal use because a still screen costs very little, and
visitors see an ngrok warning page once per browser before the app loads.
Both are covered in [docs/tunnels.md](docs/tunnels.md), along with a
Cloudflare option that has neither limit if you own a domain.

## What you get

- **Low latency.** H.264 over WebRTC peer-to-peer when both ends can reach
  each other, an encrypted WebSocket relay when they cannot.
- **Full control.** Pointer, keyboard, scroll, right click, Mission Control,
  Cmd-Tab, and copy/paste in both directions.
- **Sound**, on request — the speaker button shares whatever the Mac is playing.
- **Touch gestures** that match a trackpad: one finger taps and drags, two
  fingers scroll, two-finger tap right-clicks, three fingers switch spaces.
- **It survives being left alone.** The Mac will not sleep while someone is
  watching, the tunnel reconnects by itself, and the code stays valid until
  you stop the tool.

## The toolbar

<p align="center">
  <img src="https://raw.githubusercontent.com/jainsiddharth99/machop/main/docs/images/toolbar.png"
       alt="The Machop toolbar and the overflow menu of extra keys"
       width="760">
</p>

| | |
|---|---|
| `cmd` `tab` `⌘⇥` | modifier, key, and the app switcher held open |
| `esc` `^C` | escape, and interrupt |
| `copy` `paste` | clipboard, both directions |
| `1:1` | magnify, and re-ask the Mac for matching resolution |
| `🔊` | share the Mac's sound |
| `⌨` | soft keyboard |
| `⋯` | arrows, ctrl/opt/shift, quality, scroll speed, fullscreen |

## Typing on it

The soft keyboard works, so you can run commands from a phone — which is the
difference between watching a job fail and fixing it.

<p align="center">
  <img src="https://raw.githubusercontent.com/jainsiddharth99/machop/main/docs/images/keyboard.png"
       alt="Typing a command into the Mac's terminal from an iPhone keyboard"
       width="330">
  <img src="https://raw.githubusercontent.com/jainsiddharth99/machop/main/docs/images/connect.png"
       alt="The connect screen asking for the six-digit session code"
       width="330">
</p>

## Requirements

macOS 13 or later, Python 3.10+. Grant Screen Recording and Accessibility
when macOS asks — Machop checks and tells you what is missing.

## Options

| flag | default | |
|---|---|---|
| `--ngrok-domain` | — | your reserved ngrok domain, for the same URL every run |
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
python3 -m venv .venv && source .venv/bin/activate
pip install -U pip
pip install -e ".[dev]"
pytest
node --test tests/web/*.test.mjs
```

The `pip install -U pip` is not cosmetic: editable installs from a
`pyproject.toml` need pip 21.3 or newer, and a system Python shipping an older
one fails with "File setup.py or setup.cfg not found".

The suite negotiates real WebRTC sessions against real screen capture, runs
a live encrypted relay, and checks decoded pixels rather than packet counts.
Tests that need a real display are skipped automatically on build machines,
so run the full suite on a Mac before opening a pull request.

## Licence

MIT. See [LICENSE](LICENSE).
