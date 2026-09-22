"""ntfy push. Never hits the real service; the base URL is injectable."""

import stat

from aiohttp import web

from machop.notify import generate_topic, load_or_create_topic, push


def test_topic_is_long_and_prefixed():
    topic = generate_topic()
    assert topic.startswith("mm-")
    assert len(topic) >= 24


def test_topics_are_unique():
    assert len({generate_topic() for _ in range(50)}) == 50


def test_topic_persists_across_calls(tmp_path):
    config = tmp_path / "config.json"
    first = load_or_create_topic(config)
    assert load_or_create_topic(config) == first, "user must only subscribe once"


def test_corrupt_config_is_replaced_not_fatal(tmp_path):
    config = tmp_path / "config.json"
    config.write_text("{ not json")
    assert load_or_create_topic(config).startswith("mm-")


def test_config_file_is_not_world_readable(tmp_path):
    config = tmp_path / "config.json"
    load_or_create_topic(config)
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


async def test_push_posts_the_message(aiohttp_server):
    received = {}

    async def handler(request: web.Request) -> web.Response:
        received["path"] = request.path
        received["body"] = await request.text()
        received["title"] = request.headers.get("Title")
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_post("/{topic}", handler)
    server = await aiohttp_server(app)
    base = f"http://127.0.0.1:{server.port}"

    assert await push("mm-abc123", "https://new.example", base_url=base) is True
    assert received["path"] == "/mm-abc123"
    assert "https://new.example" in received["body"]
    assert received["title"] == "Machop"


async def test_push_failure_is_reported_not_raised():
    """A notification must never take down a working session."""
    assert await push("mm-abc", "hi", base_url="http://127.0.0.1:1") is False


async def test_server_error_is_reported_as_failure(aiohttp_server):
    async def handler(request: web.Request) -> web.Response:
        return web.Response(status=500)

    app = web.Application()
    app.router.add_post("/{topic}", handler)
    server = await aiohttp_server(app)
    assert await push("t", "m", base_url=f"http://127.0.0.1:{server.port}") is False
