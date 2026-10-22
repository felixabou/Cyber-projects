from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

from memecoin_agent.data.pumpportal import FeedAugmentedMarket, PumpPortalFeed

from .conftest import make_snap


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def migrate(mint, symbol=""):
    return json.dumps({"signature": "sig", "mint": mint, "txType": "migrate", "pool": "pump-amm", "symbol": symbol})


def create(mint):
    return json.dumps({"signature": "sig", "mint": mint, "txType": "create", "name": "X", "symbol": "X",
                       "marketCapSol": 30.1, "pool": "pump"})


def test_message_parsing_and_ordering():
    clock = Clock()
    feed = PumpPortalFeed(clock=clock)
    assert feed.handle_message(json.dumps({"message": "Successfully subscribed to token creation events."})) is None
    assert feed.handle_message("not json") is None
    assert feed.handle_message(json.dumps({"mint": "Z", "txType": "buy"})) is None

    feed.handle_message(create("L1"))
    clock.t += 1
    feed.handle_message(migrate("G1"))
    clock.t += 1
    feed.handle_message(create("L2"))
    clock.t += 1
    feed.handle_message(migrate("G2"))
    assert feed.recent() == ["G2", "G1", "L2", "L1"]  # graduations first, newest first

    feed.handle_message(create("G1"))  # a later "create" for a graduated token can't downgrade it
    assert feed.recent()[:2] == ["G2", "G1"]


def test_buffer_bounds_and_persistence(tmp_path):
    clock = Clock()
    path = tmp_path / "feed.json"
    feed = PumpPortalFeed(clock=clock, max_age_s=3600, max_mints=3, buffer_file=path)
    for i in range(5):
        feed.handle_message(migrate(f"M{i}"))
    assert feed.recent() == ["M4", "M3", "M2"]

    feed.save()
    clock.t += 1800
    reloaded = PumpPortalFeed(clock=clock, max_age_s=3600, buffer_file=path)
    reloaded.load()
    assert reloaded.recent() == ["M4", "M3", "M2"]
    clock.t += 1801
    assert reloaded.recent() == []  # aged out


def test_feed_mints_lead_discovery_and_dead_pools_are_evicted():
    class Base:
        def discover(self):
            return ["DEX1", "G1"]

        def snapshots(self, mints):
            return {"G1": make_snap(mint="G1", liquidity_usd=1_000), "G2": make_snap(mint="G2")}

        def sol_price_usd(self):
            return 150.0

    clock = Clock()
    feed = PumpPortalFeed(clock=clock)
    feed.handle_message(migrate("G1"))
    feed.handle_message(migrate("G2"))
    market = FeedAugmentedMarket(Base(), feed, evict_below_liquidity_usd=5_000, evict_grace_s=1800)
    assert market.discover()[-1] == "DEX1" and set(market.discover()[:2]) == {"G1", "G2"}

    market.snapshots(["G1", "G2"])
    assert "G1" in feed.recent()  # within the grace period: the new pool may not be indexed yet
    clock.t += 1801
    market.snapshots(["G1", "G2"])
    assert feed.recent() == ["G2"]
    assert market.discover() == ["G2", "DEX1", "G1"]  # still discoverable via the base feed


def test_websocket_subscribe_receive_and_reconnect():
    """Real websocket client against a local server: subscribes, buffers events, reconnects after a drop."""
    pytest.importorskip("websockets")
    from websockets.asyncio.server import serve

    received: list[str] = []
    connections = 0
    ready = threading.Event()
    port_box: list[int] = []
    stop_server = threading.Event()

    async def handler(ws):
        nonlocal connections
        connections += 1
        n = connections
        received.append(await ws.recv())
        await ws.send(json.dumps({"message": "Successfully subscribed to migration events."}))
        await ws.send(migrate(f"CONN{n}"))
        if n == 1:
            await ws.close()  # simulate the server dropping us
            return
        await ws.wait_closed()

    async def server_main():
        async with serve(handler, "127.0.0.1", 0) as server:
            port_box.append(server.sockets[0].getsockname()[1])
            ready.set()
            while not stop_server.is_set():
                await asyncio.sleep(0.05)

    t = threading.Thread(target=lambda: asyncio.run(server_main()), daemon=True)
    t.start()
    assert ready.wait(5)

    feed = PumpPortalFeed(url=f"ws://127.0.0.1:{port_box[0]}", connect_kwargs={"proxy": None})
    feed.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and set(feed.recent()) != {"CONN1", "CONN2"}:
            time.sleep(0.05)
        assert set(feed.recent()) == {"CONN1", "CONN2"}
        assert connections == 2
        assert all(json.loads(m) == {"method": "subscribeMigration"} for m in received)
    finally:
        feed.stop()
        stop_server.set()
        t.join(5)
    assert not feed.connected.is_set()
