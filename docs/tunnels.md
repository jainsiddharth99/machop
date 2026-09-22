# Tunnels and permanent URLs

## Which tunnel

`--tunnel auto` (the default) uses **cloudflared** when it is installed and
falls back to **localhost.run** when it is not. Measured signalling latency:

| backend | per request | stable URL | needs |
|---|---|---|---|
| cloudflared | **~0.10 s** | no | `brew install cloudflared` |
| localhost.run | ~1.21 s | no | nothing - `ssh` ships with macOS |
| ngrok | not measured here | **yes**, with a warning page | a free ngrok account |
| cloudflare-named | not measured here | **yes**, clean | a domain on Cloudflare (~$10/yr) |

The two latency figures are measured on this machine; the ngrok row is not,
because benchmarking it needs an account this project does not have.

On the peer-to-peer path the tunnel only carries the page and one SDP
exchange, so this affects how quickly the viewer loads and connects rather
than video smoothness. It matters much more on the encrypted relay fallback,
where every frame crosses it.

cloudflared and localhost.run both hand out a fresh random hostname every
run. Cloudflare's own banner notes that quick tunnels have no uptime
guarantee, and creating many in quick succession appears to rate limit. The
supervisor reconnects either way; pair it with `--notify` so a new URL
reaches you rather than locking you out.

### A permanent URL

ngrok's free plan includes one **dev domain** - a fixed `*.ngrok-free.app`
address that ngrok assigns to your account. You cannot choose the name on the
free plan, but it never changes, which is the part that matters: the address
and the session code both stay the same, so the page can be bookmarked on a
phone and never re-shared.

Set-up is once, not per run. **Machop starts and stops `ngrok` itself**,
the same way it already does with `cloudflared`:

```bash
brew install ngrok
ngrok config add-authtoken <token from dashboard.ngrok.com>
# copy your assigned domain from dashboard.ngrok.com -> Domains, then:
machop --ngrok-domain your-assigned-name.ngrok-free.app
```

`--ngrok-domain` implies `--tunnel ngrok`.

**The authtoken is not optional.** ngrok caps *unauthenticated* tunnels at two
hours; an authenticated one, free accounts included, has no session timeout
and can stay up as long as the tool runs.

Unlike Tailscale, ngrok opens no network interface and installs no daemon, so
it cannot collide with a corporate VPN's routing table.

#### The 1 GB/month cap, in practice

Video does not cross the tunnel on the peer-to-peer path - it goes directly
between the two devices. The tunnel carries the page and one handshake.
Measured here:

| what | bytes |
|---|---|
| `index.html` | 5,255 |
| `app.js` (cached after the first visit) | 36,211 |
| SDP offer + answer | 6,808 |
| **a reconnect, warm cache** | **~12 KB** |

So two hours a day for 21 days is about **0.29 MB a month - 0.03% of the
allowance.** Session length costs nothing; only connecting does. You would
have to reconnect roughly 80,000 times to reach the cap.

The exception is the **encrypted relay**, where every frame does cross the
tunnel. That sounds fatal and mostly is not, because 4 Mbps is a ceiling
rather than a rate - screen content only spends it while the screen is
changing. Measured on this Mac at 1290x838:

| what you are doing | rate | hours per GB |
|---|---|---|
| tab hidden (phone in a pocket) | **0** | unlimited |
| reading a mostly still screen | ~4 KB/s | **~67** |
| continuous full-screen motion | up to 4 Mbps | ~0.6 |

The first row is not a rounding: the viewer tells the Mac when its tab goes
off screen and the Mac stops encoding entirely, so a session left open all
day costs nothing while nobody is looking at it.

Checking on a deployment is the first row. You would have to scroll without
stopping for half an hour to spend the allowance. The Mac prints a warning
when the relay engages either way, and the viewer shows `relay` in the
status bar. `--no-relay` refuses the path outright, and `--relay-bitrate`
lowers the ceiling.

#### The interstitial, and why it cannot be removed

Free ngrok domains serve a warning page to browsers on the first HTML
request - you tap "Visit Site" and carry on. It does not touch the
signalling or the video, only that first page load, and ngrok sets a cookie
so it appears once per browser rather than once per session.

There is no way around it on a free account, and the page's own advice does
not apply here:

- **The `ngrok-skip-browser-warning` header.** ngrok's own docs for the
  add-headers action say: *"You may not use this action to add the
  `ngrok-skip-browser-warning` header to skip the ngrok browser warning on
  free accounts."* So a traffic policy cannot do it either.
- **A custom User-Agent.** A browser typing a URL into the address bar sends
  its own; nothing on the Mac can change that.

Both suggestions assume a program making the request, not a person opening a
link. Removing it properly means a paid ngrok plan, or the next section.

### A permanent URL with no warning page

A Cloudflare **named** tunnel. No interstitial, no bandwidth cap (so no
relay-fallback hazard either), and the URL is yours. The cost is a domain on
Cloudflare DNS, roughly $10 a year.

```bash
brew install cloudflared
cloudflared tunnel login
cloudflared tunnel create machop
cloudflared tunnel route dns machop mac.example.com
```

Then, every time:

```bash
machop --cloudflare-tunnel machop --cloudflare-hostname mac.example.com
```

`--cloudflare-tunnel` implies `--tunnel cloudflare-named`. As with the other
backends, Machop starts and stops `cloudflared` itself.

Unlike a quick tunnel there is no wait for DNS to propagate - the record was
created once, at set-up - so the URL works the moment it is printed.

If you would rather not have the warning page at all, see the Cloudflare
named tunnel below.

There is deliberately **no Tailscale backend.** Tailscale Funnel would give a
permanent URL and terminate TLS on your own Mac, but it needs an account, and
its `100.64.0.0/10` range overlaps what other WireGuard VPNs (NetBird among
them) already use - risking a work VPN for a URL that `--notify` makes
unnecessary.
