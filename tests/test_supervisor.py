"""Reconnection, using a fake backend so failures are deterministic."""

import asyncio

import pytest

from machop.tunnels.base import Tunnel, TunnelError
from machop.tunnels.supervisor import TunnelSupervisor


class FakeTunnel(Tunnel):
    name = "fake"
    urls = ["https://one.example", "https://two.example", "https://three.example"]
    instances = 0
    fail_next = 0

    def __init__(self) -> None:
        super().__init__()
        self.stopped = False

    async def start(self, local_port: int, on_url=None) -> str:
        if FakeTunnel.fail_next > 0:
            FakeTunnel.fail_next -= 1
            raise TunnelError("simulated failure")
        index = FakeTunnel.instances
        FakeTunnel.instances += 1
        self.public_url = FakeTunnel.urls[index % len(FakeTunnel.urls)]
        return self.public_url

    async def stop(self) -> None:
        self.stopped = True


@pytest.fixture(autouse=True)
def reset_fake():
    FakeTunnel.instances = 0
    FakeTunnel.fail_next = 0
    yield
    FakeTunnel.instances = 0
    FakeTunnel.fail_next = 0


async def _until(predicate, interval: float = 0.02) -> None:
    while not predicate():
        await asyncio.sleep(interval)


async def test_start_returns_the_first_url():
    supervisor = TunnelSupervisor(FakeTunnel)
    try:
        assert await supervisor.start(8080) == "https://one.example"
        assert supervisor.public_url == "https://one.example"
    finally:
        await supervisor.stop()


async def test_loss_triggers_reconnect_and_reports_the_new_url():
    seen: list[str] = []
    supervisor = TunnelSupervisor(FakeTunnel, on_url_change=seen.append, max_backoff=0.05)
    try:
        await supervisor.start(8080)
        supervisor._tunnel.lost.set()
        await asyncio.wait_for(_until(lambda: seen), timeout=5)
        assert seen == ["https://two.example"]
        assert supervisor.public_url == "https://two.example"
    finally:
        await supervisor.stop()


async def test_initial_failure_is_raised_not_retried():
    """Startup must fail loudly; only losses mid-session are retried."""
    FakeTunnel.fail_next = 1
    supervisor = TunnelSupervisor(FakeTunnel, max_backoff=0.05)
    try:
        with pytest.raises(TunnelError):
            await supervisor.start(8080)
    finally:
        await supervisor.stop()


async def test_reconnect_retries_until_it_succeeds():
    seen: list[str] = []
    supervisor = TunnelSupervisor(FakeTunnel, on_url_change=seen.append, max_backoff=0.05)
    try:
        await supervisor.start(8080)
        FakeTunnel.fail_next = 2
        supervisor._tunnel.lost.set()
        await asyncio.wait_for(_until(lambda: seen), timeout=10)
        assert seen == ["https://two.example"]
    finally:
        await supervisor.stop()


async def test_the_old_tunnel_is_stopped_before_reconnecting():
    """Otherwise every reconnect leaks an ssh process."""
    supervisor = TunnelSupervisor(FakeTunnel, max_backoff=0.05)
    try:
        await supervisor.start(8080)
        first = supervisor._tunnel
        first.lost.set()
        await asyncio.wait_for(_until(lambda: supervisor._tunnel is not first), timeout=5)
        assert first.stopped
    finally:
        await supervisor.stop()


async def test_stop_is_idempotent():
    supervisor = TunnelSupervisor(FakeTunnel)
    await supervisor.start(8080)
    await supervisor.stop()
    await supervisor.stop()


async def test_stop_without_start_is_safe():
    await TunnelSupervisor(FakeTunnel).stop()


async def test_the_supervisor_passes_the_early_url_callback_through():
    """The banner is printed from this callback. A supervisor that quietly
    dropped it would leave the terminal blank for the ten seconds a quick
    tunnel spends waiting on DNS, which is the whole problem it solves."""
    seen = []

    class Recording(FakeTunnel):
        async def start(self, local_port: int, on_url=None) -> str:
            if on_url is not None:
                on_url("https://early.example")
            return await super().start(local_port)

    FakeTunnel.instances = 0
    supervisor = TunnelSupervisor(Recording)
    await supervisor.start(1234, on_url=seen.append)
    await supervisor.stop()
    assert seen == ["https://early.example"]


def test_kill_now_reaches_the_child_without_an_event_loop():
    """Called from a signal handler on the way to os._exit, where there is
    no loop left to await stop() on."""
    killed = []

    class Child:
        returncode = None

        def kill(self):
            killed.append(True)

    tunnel = FakeTunnel()
    tunnel._process = Child()
    supervisor = TunnelSupervisor(lambda: tunnel)
    supervisor._tunnel = tunnel
    supervisor.kill_now()
    assert killed == [True]


def test_kill_now_is_safe_when_nothing_is_running():
    TunnelSupervisor(FakeTunnel).kill_now()
