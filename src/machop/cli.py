"""Command line entry point and orchestration."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys

from . import __version__
from .tunnels import (
    BACKENDS,
    TunnelError,
    TunnelSupervisor,
    create_tunnel,
    resolve_backend,
)

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8080
DEFAULT_MAX_WIDTH = 1440
DEFAULT_FPS = 60
DEFAULT_BITRATE_MBPS = 8.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="machop",
        description="Stream this Mac to any browser and control it remotely.",
    )
    parser.add_argument("--version", action="version", version=f"machop {__version__}")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"local signalling port (default: {DEFAULT_PORT})")
    parser.add_argument("--pin", default=None,
                        help="session code (default: a fresh random 6-digit code)")
    parser.add_argument("--max-width", type=int, default=DEFAULT_MAX_WIDTH,
                        help=f"capture width in pixels (default: {DEFAULT_MAX_WIDTH})")
    parser.add_argument("--fps", type=int, default=DEFAULT_FPS,
                        help=f"maximum frame rate (default: {DEFAULT_FPS})")
    parser.add_argument("--bitrate", type=float, default=DEFAULT_BITRATE_MBPS,
                        help=f"video ceiling in Mbps (default: {DEFAULT_BITRATE_MBPS})")
    parser.add_argument("--min-bitrate", type=float, metavar="MBPS",
                        help="floor for the bitrate the viewer's browser asks "
                             "for (default: a quarter of the profile, 1-2 Mbps). "
                             "Lower it on a genuinely slow link")
    parser.add_argument("--relay-bitrate", type=float, default=4.0,
                        help="fallback relay ceiling in Mbps (default: 4.0)")
    parser.add_argument("--no-relay", action="store_true",
                        help="peer-to-peer only; never fall back to the relay, "
                             "so screen data can never transit a third party")
    parser.add_argument("--prefer", choices=("auto", "relay"), default="auto",
                        help="auto tries a direct connection first and falls "
                             "back to the encrypted relay in about a second, "
                             "so the same link works on wifi and on mobile "
                             "data; relay skips the attempt entirely "
                             "(default: auto)")
    parser.add_argument("--no-audio", action="store_true",
                        help="never share this Mac's sound, even if the "
                             "viewer asks. Sound is off until the viewer "
                             "presses the speaker button either way")
    parser.add_argument("--pin-ttl", type=float, default=None,
                        help="expire an unused code after N seconds "
                             "(default: never, so you can connect hours later)")
    parser.add_argument("--all-interfaces", action="store_true",
                        help="gather ICE from every local interface; adds ~5s "
                             "to every connection but keeps VPN paths (e.g. a "
                             "corporate WireGuard tunnel) usable for peer-to-peer")
    parser.add_argument("--profile", choices=("text", "gui"), default="gui",
                        help="quality profile to start in: text is sharp and "
                             "cheap, gui is smoother for pointer work "
                             "(default: gui)")
    parser.add_argument("--view-only", action="store_true",
                        help="stream the screen but ignore all remote input")
    parser.add_argument("--no-cursor", action="store_true",
                        help="exclude the pointer from the captured image")
    parser.add_argument("--notify", action="store_true",
                        help="push the new address to ntfy when the tunnel "
                             "reconnects, so a URL change does not lock you out")
    parser.add_argument("--tunnel", choices=BACKENDS, default="auto",
                        help="tunnel backend (default: auto - cloudflared if "
                             "installed, else localhost.run)")
    parser.add_argument("--ngrok-domain", metavar="HOST",
                        help="reserved ngrok domain to get the SAME URL every "
                             "run, e.g. your-name.ngrok-free.app (implies "
                             "--tunnel ngrok; free at dashboard.ngrok.com)")
    parser.add_argument("--cloudflare-tunnel", metavar="NAME",
                        help="named Cloudflare tunnel for a permanent URL with "
                             "no warning page and no bandwidth cap (needs a "
                             "domain on Cloudflare DNS; implies "
                             "--tunnel cloudflare-named)")
    parser.add_argument("--cloudflare-hostname", metavar="HOST",
                        help="the hostname routed to that tunnel, "
                             "e.g. mac.example.com")
    parser.add_argument("--no-tunnel", action="store_true",
                        help="serve on localhost only; do not open a public URL")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser


def _banner(
    url: str,
    pin: str,
    geometry,
    capture_size,
    view_only: bool,
    relay: str,
    sound: str = "on request (the speaker button in the viewer)",
) -> str:
    width, height = capture_size
    lines = [
        "",
        "  Machop is live",
        "",
        f"    Open      {url}",
        f"    Code      {pin}",
        "",
        f"    Display   {geometry.width}x{geometry.height} pt  ->  streaming {width}x{height}",
        f"    Input     {'disabled (view only)' if view_only else 'enabled'}",
        f"    Sound     {sound}",
        f"    Fallback  {relay}",
        "",
        "    The code stays valid until you stop the tool, and survives a",
        "    tunnel reconnect. Write it down before you leave.",
        "",
        "  Press Ctrl-C to stop.",
        "",
    ]
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> int:
    """Everything heavy is imported here rather than at module scope.

    PyObjC, numpy, av and aiortc cost the better part of a second warm and
    far longer the first time macOS has to fault the frameworks in - and
    all of it happened before argparse ran, so `machop` sat completely
    silent for as long as it took. People reasonably concluded it had hung
    and pressed Ctrl-C, which landed mid-import.
    """
    from .capture import PROFILES, SwitchableSource, display_geometry, target_size
    from .ice import limit_ice_interfaces, routable_addresses
    from .media import install_tuned_encoder, tune_encoder
    from .notify import load_or_create_topic, push
    from .permissions import check_permissions, describe_missing
    from .power import PowerAssertions, startup_warnings
    from .security import SessionAuth, generate_pin
    from .server import (
        DEFAULT_ICE_SERVERS,
        SignalingServer,
        build_controller,
        start_server,
    )

    status = check_permissions(prompt=True)
    if not status.ok:
        print(describe_missing(status), file=sys.stderr)
        return 1

    pin = args.pin or generate_pin()
    geometry = display_geometry()
    profile_width, profile_bitrate = PROFILES[args.profile]
    capture_size = target_size(geometry, min(args.max_width, profile_width))

    bitrate = (
        int(args.bitrate * 1_000_000)
        if args.bitrate != DEFAULT_BITRATE_MBPS
        else profile_bitrate
    )
    floor = int(args.min_bitrate * 1_000_000) if args.min_bitrate else None
    tune_encoder(bitrate, floor)
    install_tuned_encoder()

    ice_servers = list(DEFAULT_ICE_SERVERS)
    if not args.all_interfaces and ice_servers:
        limit_ice_interfaces(await routable_addresses(ice_servers[0], timeout=1.0))

    source = SwitchableSource(
        capture_size[0], capture_size[1], args.fps, show_cursor=not args.no_cursor
    )
    await source.start()
    controller = build_controller()
    controller.enabled = not args.view_only

    power = PowerAssertions()
    power.hold_system()

    def viewer_active() -> None:
        power.hold_display()
        power.wake_display()

    warned_about_relay = False

    def relay_active(forced: bool = False) -> None:
        """Say it on screen, once."""
        nonlocal warned_about_relay
        if warned_about_relay:
            return
        warned_about_relay = True
        gb_per_hour = args.relay_bitrate * 3600 / 8 / 1000
        headline = (
            "Using the encrypted relay, as the viewer asked."
            if forced
            else "Peer-to-peer failed; using the encrypted relay instead."
        )
        print(
            f"\n  ! {headline}"
            f"\n    Video crosses the tunnel now. A still screen costs very "
            f"little\n    (measured ~4 KB/s, around 60 hours per GB); "
            f"continuous motion can\n    reach {args.relay_bitrate:.0f} Mbps "
            f"(~{gb_per_hour:.1f} GB/hour). Matters only on a metered\n    "
            f"tunnel such as ngrok's free plan. --no-relay refuses this path.\n",
            file=sys.stderr, flush=True,
        )

    stop_event = asyncio.Event()
    server = SignalingServer(
        auth=SessionAuth(pin=pin, ttl=args.pin_ttl),
        source=source,
        controller=controller,
        relay_bitrate=int(args.relay_bitrate * 1_000_000),
        relay_enabled=not args.no_relay,
        audio_allowed=not args.no_audio,
        max_width=args.max_width,
        profile=args.profile,
        min_bitrate=floor,
        ice_servers=ice_servers,
        on_viewer_active=viewer_active,
        on_viewer_idle=power.release_display,
        on_relay_active=relay_active,
    )

    runner = await start_server(server, port=args.port, host="127.0.0.1")
    topic = load_or_create_topic() if args.notify else None

    def announce_url(url: str) -> None:
        print(f"\n  Tunnel reconnected. New address: {url}\n", flush=True)
        if topic is not None:
            asyncio.create_task(push(topic, url))

    tunnel = TunnelSupervisor(
        create_tunnel(
            args.tunnel,
            domain=args.ngrok_domain,
            cf_tunnel=args.cloudflare_tunnel,
            cf_hostname=args.cloudflare_hostname,
        ),
        on_url_change=announce_url,
    )

    loop = asyncio.get_running_loop()
    interrupts = 0

    def request_stop() -> None:
        """First Ctrl-C asks nicely; a second one stops arguing.

        Whatever else is skipped on the way out, the power assertions are
        released and the tunnel child is killed: a leaked assertion keeps
        this Mac awake indefinitely, and an orphaned cloudflared keeps a
        tunnel open to a server that has gone.
        """
        nonlocal interrupts
        interrupts += 1
        stop_event.set()
        if interrupts >= 2:
            print("\n  Stopping now.", file=sys.stderr, flush=True)
            power.release_all()
            tunnel.kill_now()
            os._exit(130)

    for signame in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signame, request_stop)

    exit_code = 0
    try:
        announced = False

        def show(address: str, live: bool) -> None:
            """Print the banner as soon as the address exists, not when it
            starts working. A quick Cloudflare tunnel spends about ten
            seconds waiting for DNS, and the code is useful immediately."""
            nonlocal announced
            if announced:
                return
            announced = True
            print(
                _banner(
                    viewer_url(address, args.prefer), pin, geometry, capture_size,
                    args.view_only, relay_note,
                    "never (--no-audio)" if args.no_audio
                    else "on request (the speaker button in the viewer)",
                ),
                flush=True,
            )
            if not live:
                print(
                    "  The address is still being published in DNS; it will "
                    "start\n  answering in a few seconds.\n",
                    file=sys.stderr, flush=True,
                )

        relay_note = (
            "disabled (peer-to-peer only)" if args.no_relay
            else "encrypted relay straight away (--prefer relay)"
            if args.prefer == "relay"
            else "encrypted relay if a direct connection does not come up"
        )
        if args.no_tunnel:
            url = f"http://127.0.0.1:{args.port}/"
            show(url, live=True)
        else:
            backend = resolve_backend(args.tunnel)
            print(f"  Opening secure tunnel via {backend}…", file=sys.stderr, flush=True)
            try:
                url = await until_stopped(
                    tunnel.start(
                        local_port=args.port,
                        on_url=lambda address: show(address, live=False),
                    ),
                    stop_event,
                )
            except TunnelError as exc:
                # `auto` means "give me a tunnel", not "give me cloudflared
                # and fail if it is having a bad day". localhost.run needs
                # nothing installed, so there is always somewhere to go.
                if not should_fall_back(args.tunnel, backend):
                    raise
                print(
                    f"  {backend} did not come up ({exc});\n"
                    f"  falling back to localhost.run…",
                    file=sys.stderr, flush=True,
                )
                await tunnel.stop()
                tunnel = TunnelSupervisor(
                    create_tunnel("localhost.run"), on_url_change=announce_url
                )
                url = await until_stopped(
                    tunnel.start(
                        local_port=args.port,
                        on_url=lambda address: show(address, live=False),
                    ),
                    stop_event,
                )
            show(url, live=True)
            print("  The address is live.\n", file=sys.stderr, flush=True)
        url = viewer_url(url, args.prefer)
        if topic is not None:
            print(
                f"    Alerts    subscribe at https://ntfy.sh/{topic}\n",
                flush=True,
            )
        for warning in startup_warnings():
            print(f"  ! {warning}", file=sys.stderr, flush=True)
        print("", file=sys.stderr, flush=True)
        await stop_event.wait()
    except _Interrupted:
        exit_code = 130
    except TunnelError as exc:
        print(f"\n  Could not open the tunnel: {exc}\n", file=sys.stderr)
        exit_code = 1
    finally:
        print("\n  Shutting down…", file=sys.stderr)
        power.release_all()
        await tunnel.stop()
        await server.close()
        await runner.cleanup()
        await source.stop()
        controller.release_all()
    return exit_code


def should_fall_back(requested: str, backend: str) -> bool:
    """Whether a failed tunnel should be retried on localhost.run.

    Only for `auto`, which means "give me a tunnel" rather than "give me
    cloudflared and fail if it is having a bad day". An explicitly named
    backend is a decision, and silently substituting another one would hide
    exactly the thing the user asked to control.
    """
    return requested == "auto" and backend != "localhost.run"


class _Interrupted(Exception):
    """Ctrl-C arrived while the tool was still starting up."""


async def until_stopped(awaitable, stop_event: asyncio.Event):
    """Await something, but abandon it the moment Ctrl-C is pressed.

    `loop.add_signal_handler` REPLACES the default SIGINT behaviour, so
    Ctrl-C stops raising KeyboardInterrupt and only sets an event. Anything
    awaited during startup therefore has to race that event, or the process
    is deaf to Ctrl-C until startup finishes - which, while a quick tunnel
    waits on DNS, meant holding Ctrl-C down and eventually killing the
    terminal.
    """
    task = asyncio.ensure_future(awaitable)
    waiter = asyncio.ensure_future(stop_event.wait())
    try:
        await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if not task.done():
            raise _Interrupted
        return task.result()
    finally:
        for pending in (task, waiter):
            if not pending.done():
                pending.cancel()
                with contextlib.suppress(BaseException):
                    await pending


def viewer_url(url: str, prefer: str) -> str:
    """The address to hand the user, given how they want to connect."""
    if prefer != "relay":
        return url
    if "relay=" in url:
        return url
    return url + ("&" if "?" in url else "?") + "relay=1"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if sys.platform != "darwin":
        print(
            "Machop captures a Mac's screen, so it only runs on macOS.\n"
            f"This is {sys.platform}.",
            file=sys.stderr,
        )
        return 1
    if args.ngrok_domain and args.tunnel == "auto":
        args.tunnel = "ngrok"
    if args.cloudflare_tunnel and args.tunnel == "auto":
        args.tunnel = "cloudflare-named"
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s" if args.verbose else "  %(message)s",
    )
    if not args.verbose:
        logging.getLogger("aioice").setLevel(logging.WARNING)
        logging.getLogger("aiortc").setLevel(logging.WARNING)
    print("  Starting up…", file=sys.stderr, flush=True)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
