"""SanAlert's local MCP server: the level for one place, with the place kept on this computer.

The server's own Feed talks to a fake API (httpx.MockTransport), so every request it would send is
seen here and nothing goes online. The tool is called through the SDK's own client, as an assistant
calls it."""
import datetime as dt
import json
import pathlib
import subprocess
import sys

import httpx
import pytest
from mcp import Client

from sanalert_mcp import __version__, core, server

NOW = dt.datetime(2026, 10, 20, 21, 0, tzinfo=dt.timezone.utc)
PRZEMYSL = next(t for t in server.TOWNS if t["name"] == "Przemyśl")
PRZEMYSL_CELL = core.cell_id(PRZEMYSL["lat"], PRZEMYSL["lon"])
NEPTUN_TEXT = "Дані: Карта повітряних тривог — NEPTUN"
GRID = {"v": 1, "cells": {PRZEMYSL_CELL: "podkarpackie", core.cell_id(51.25, 22.57): "lubelskie"}}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Nothing in these tests may reach a real network: the fake API is the only way out."""
    import socket

    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to reach the network")
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def a_state(age_s: float = 5, **over) -> dict:
    feed = {"status": "ok", "age_s": 3, "reason": None}
    doc = {"v": 1, "generated_at": iso(NOW - dt.timedelta(seconds=age_s)), "levels_valid": True,
           "health": {"status": "ok", "reasons": [], "sources": {n: dict(feed) for n in ("neptun", "rcb", "rso")}},
           "cells": {}, "official": [], "notice": server.TEXTS["notice"], "attribution": server.TEXTS["attribution"]}
    doc.update(over)
    return doc


NEAR = {PRZEMYSL_CELL: {"level": "near", "risk": 90, "confidence": "high",
                        "drivers": [{"code": "object_near", "kind": "drone", "km": 12, "oblast": "lviv"}]}}


class FakeAPI:
    """The two documents, with ETag and 304; every request recorded; failures on demand."""

    def __init__(self, state: dict | None = None, grid: dict | None = GRID, fail: set[str] = frozenset(),
                 status: int = 200, raw: bytes | None = None):
        self.state, self.grid, self.fail = state or a_state(), grid, set(fail)
        self.status, self.raw = status, raw
        self.requests: list[httpx.Request] = []
        self.etag = '"s1"'

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url in self.fail:
            raise httpx.ConnectError("down", request=request)
        if url == server.STATE_URL:
            if request.headers.get("If-None-Match") == self.etag:
                return httpx.Response(304, headers={"ETag": self.etag})
            if self.status != 200:
                return httpx.Response(self.status)
            if self.raw is not None:
                return httpx.Response(200, content=self.raw, headers={"Content-Type": "application/json"})
            return httpx.Response(200, json=self.state, headers={"ETag": self.etag})
        if url == server.GRID_URL and self.grid is not None:
            return httpx.Response(200, json=self.grid)
        return httpx.Response(404)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def setup(api: FakeAPI, clock: Clock | None = None, wall: Clock | None = None, now=None):
    feed = server.Feed(transport=httpx.MockTransport(api), clock=clock or Clock(), wall=wall or Clock())
    return server.build(feed, now=now or (lambda: NOW))


async def call(mcp, **args):
    async with Client(mcp) as client:
        result = await client.call_tool("sanalert_level", args)
    return result


async def level(mcp, **args) -> dict:
    result = await call(mcp, **args)
    assert not result.is_error, result.content
    return result.structured_content


async def test_one_tool_read_only_that_takes_a_town_or_coordinates():
    async with Client(setup(FakeAPI())) as client:
        tools = (await client.list_tools()).tools
        assert client.server_info.version == __version__
        assert "never sends the place anywhere" in client.instructions
    assert [t.name for t in tools] == ["sanalert_level"]
    tool = tools[0]
    assert tool.annotations.read_only_hint and not tool.annotations.destructive_hint
    assert set(tool.input_schema["properties"]) == {"place", "latitude", "longitude", "language"}


async def test_a_town_by_name_gets_its_level_and_the_websites_sentence():
    api = FakeAPI(a_state(cells=NEAR))
    out = await level(setup(api), place="przemysl")          # Polish letters optional
    assert out["place"] == {"name": "Przemyśl", "latitude": PRZEMYSL["lat"], "longitude": PRZEMYSL["lon"], "cell": PRZEMYSL_CELL,
                            "voivodeship": "podkarpackie"}
    assert (out["level"], out["level_name"], out["data"]) == ("near", "Near", "ok")
    assert out["say"] == core.explain("en", "near", NEAR[PRZEMYSL_CELL]["drivers"]) == "Drone is near, about 12 km from here."
    assert (out["reason_code"], out["kind"], out["km"], out["risk"], out["confidence"]) == ("object_near", "drone", 12, 90, "high")
    assert out["no_data_reasons"] == [] and out["official"] == []


async def test_coordinates_give_the_same_answer_and_the_town_name_stays_empty():
    out = await level(setup(FakeAPI(a_state(cells=NEAR))), latitude=PRZEMYSL["lat"], longitude=PRZEMYSL["lon"])
    assert out["place"]["name"] is None and out["level"] == "near"


@pytest.mark.parametrize("language,name,sentence", [
    ("pl", "Blisko", "Dron jest blisko, około 12 km stąd."),
    ("uk", "Близько", "Дрон близько, приблизно за 12 км звідси."),
])
async def test_the_level_and_the_sentence_in_polish_and_ukrainian(language, name, sentence):
    out = await level(setup(FakeAPI(a_state(cells=NEAR))), place="Przemyśl", language=language)
    assert (out["level_name"], out["say"]) == (name, sentence)


async def test_every_answer_carries_the_notice_the_attribution_and_neptuns_link():
    for api in (FakeAPI(a_state(cells=NEAR)), FakeAPI(fail={server.STATE_URL, server.GRID_URL})):
        out = await level(setup(api), place="Przemyśl")
        assert out["notice"] == server.TEXTS["notice"] and out["notice"]["en"].startswith("SanAlert is not an official")
        assert any(a["name"] == "NEPTUN" for a in out["attribution"])
        assert out["pass_through"]["neptun_link_text"] == NEPTUN_TEXT
        assert "official air-raid alarms" in out["pass_through"]["official_alarms"]
        assert any("never call the place safe" in rule for rule in out["answer_rules"])


async def test_the_place_never_leaves_this_computer():
    api = FakeAPI(a_state(cells=NEAR))
    mcp = setup(api)
    await level(mcp, place="Przemyśl")
    await level(mcp, latitude=50.06, longitude=19.94)
    assert {str(r.url) for r in api.requests} <= {server.STATE_URL, server.GRID_URL}
    for r in api.requests:
        assert r.method == "GET" and not r.url.query and not r.content
        assert r.headers["User-Agent"] == f"SanAlert-MCP/{__version__}"
        assert set(r.headers) <= {"host", "accept", "accept-encoding", "connection", "user-agent", "if-none-match"}


async def test_an_unknown_town_is_an_error_naming_the_list_and_nothing_is_fetched():
    api = FakeAPI()
    result = await call(setup(api), place="Mordor")
    assert result.is_error
    text = result.content[0].text
    assert "Przemyśl" in text and "latitude and longitude" in text and "Mordor" not in text
    assert api.requests == []


@pytest.mark.parametrize("args", [{}, {"latitude": 50.0}, {"place": "Lublin", "latitude": 51.2, "longitude": 22.5}])
async def test_a_town_or_both_coordinates_never_both(args):
    assert (await call(setup(FakeAPI()), **args)).is_error


async def test_blind_is_not_calm_a_stale_state():
    out = await level(setup(FakeAPI(a_state(age_s=61))), place="Przemyśl")
    assert (out["level"], out["data"]) == ("no_data", "no_data")
    assert out["say"] == "We have no data about objects in the air right now. That does not mean it is quiet."
    assert "the state is older than 60 seconds" in out["no_data_reasons"]
    assert out["official"] is None


@pytest.mark.parametrize("over,reason", [({"levels_valid": False}, "not valid"),
                                         ({"health": {"status": "unknown", "sources": {}}}, "sources are down")])
async def test_blind_is_not_calm_levels_not_valid_or_sources_down(over, reason):
    out = await level(setup(FakeAPI(a_state(cells=NEAR, **over))), place="Przemyśl")
    assert out["level"] == "no_data" and any(reason in r for r in out["no_data_reasons"])


async def test_blind_is_not_calm_the_api_cannot_be_reached():
    out = await level(setup(FakeAPI(fail={server.STATE_URL})), place="Przemyśl")
    assert (out["level"], out["data"]) == ("no_data", "no_data")
    assert "SanAlert's API could not be reached" in out["no_data_reasons"]


async def test_without_the_grid_it_is_no_data_never_out_of_range():
    out = await level(setup(FakeAPI(fail={server.GRID_URL})), place="Przemyśl")
    assert out["level"] == "no_data" and out["place"]["voivodeship"] is None and out["official"] is None
    assert any("grid" in r for r in out["no_data_reasons"])


async def test_a_point_outside_polands_grid_is_out_of_range_never_quiet():
    out = await level(setup(FakeAPI()), latitude=48.85, longitude=2.35)          # Paris
    assert out["level"] == "out_of_range" and out["say"] == "SanAlert works out levels for Poland only."
    assert out["official"] is None


async def test_the_state_at_most_once_every_5_s_with_if_none_match():
    api, clock = FakeAPI(), Clock()
    mcp = setup(api, clock)
    await level(mcp, place="Przemyśl")
    await level(mcp, place="Lublin")
    states = [r for r in api.requests if str(r.url) == server.STATE_URL]
    grids = [r for r in api.requests if str(r.url) == server.GRID_URL]
    assert len(states) == 1 and len(grids) == 1
    clock.t += 5
    out = await level(mcp, place="Przemyśl")
    states = [r for r in api.requests if str(r.url) == server.STATE_URL]
    assert len(states) == 2 and states[1].headers["If-None-Match"] == '"s1"'      # answered 304
    assert out["level"] == "quiet" and out["data"] == "ok"


async def test_official_messages_for_the_voivodeship_and_unknown_when_rcb_cannot_be_read():
    alert = {"source": "rcb", "class": "shelter", "title": "Alert RCB", "regions": ["podkarpackie"],
             "date": "2026-10-20", "cancelled": False, "url": "https://www.gov.pl/web/rcb/x",
             "first_seen": iso(NOW - dt.timedelta(minutes=10)), "updates": []}
    out = await level(setup(FakeAPI(a_state(official=[alert]))), place="Przemyśl")
    assert [(m["source"], m["tier"]) for m in out["official"]] == [("rcb", 3)]
    assert (await level(setup(FakeAPI(a_state(official=[alert]))), place="Lublin"))["official"] == []
    down = a_state(official=[alert])
    down["health"]["sources"]["rcb"] = {"status": "unknown", "age_s": None, "reason": "silent"}
    assert (await level(setup(FakeAPI(down)), place="Przemyśl"))["official"] is None


def test_the_copies_match_their_sources():
    """core.py and towns.json are the monorepo's own files, never edited here (scripts/sync.py)."""
    here = pathlib.Path(__file__).resolve().parents[1]
    if not (here.parent / "home-assistant").exists():
        pytest.skip("outside SanAlert's monorepo: the copies are checked there")
    done = subprocess.run([sys.executable, str(here / "scripts" / "sync.py"), "--check"], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_every_town_is_in_poland_and_unique():
    keys = [server._key(t["name"]) for t in server.TOWNS]
    assert len(keys) == len(set(keys)) and len(keys) >= 40
    assert all(49 <= t["lat"] <= 55 and 14 <= t["lon"] <= 24.2 for t in server.TOWNS)
    assert json.loads(json.dumps(server.TEXTS))["pass_through"]["neptun_url"] == "https://neptun.in.ua"


async def test_a_clock_running_behind_never_keeps_an_old_quiet_fresh():
    """The device clock 2 h behind: the server's document looks 2 h "in the future". After one good
    fetch the API goes down; once this process has held the document for 60 s it is no data."""
    behind = NOW - dt.timedelta(hours=2)
    api = FakeAPI(a_state(age_s=-7200 + 5))               # generated 5 s ago by the server's clock
    clock, wall, at = Clock(), Clock(), {"now": behind}
    mcp = setup(api, clock, wall, now=lambda: at["now"])
    assert (await level(mcp, place="Przemyśl"))["level"] == "quiet"
    api.fail.add(server.STATE_URL)
    for step in (30, 31):                                   # 30 s held: still the same document; 61 s: no data
        clock.t += step
        wall.t += step
        at["now"] += dt.timedelta(seconds=step)
        out = await level(mcp, place="Przemyśl")
    assert (out["level"], out["data"]) == ("no_data", "no_data")
    assert "no new state for more than 60 seconds" in out["no_data_reasons"]
    assert "SanAlert's API could not be reached" in out["no_data_reasons"]


async def test_a_laptop_that_slept_counts_the_wall_clock_too():
    """The monotonic clock stops while the computer sleeps; the wall clock does not, and the larger wins."""
    api, clock, wall = FakeAPI(), Clock(), Clock()
    mcp = setup(api, clock, wall)
    await level(mcp, place="Lublin")
    api.fail.add(server.STATE_URL)
    clock.t += 6                                            # slept: the monotonic clock barely moved
    wall.t += 3600
    out = await level(mcp, place="Lublin")
    assert out["level"] == "no_data" and "no new state for more than 60 seconds" in out["no_data_reasons"]


@pytest.mark.parametrize("api,reason", [
    (FakeAPI(status=503), "answered HTTP 503"),
    (FakeAPI(raw=b"{not json"), "could not be read"),
    (FakeAPI(raw=b"[1, 2]"), "could not be read"),
])
async def test_an_error_or_an_unreadable_document_is_no_data_with_its_reason(api, reason):
    out = await level(setup(api), place="Przemyśl")
    assert out["level"] == "no_data" and any(reason in r for r in out["no_data_reasons"])


async def test_a_state_with_no_time_or_an_unknown_level_says_so():
    no_time = a_state(); del no_time["generated_at"]
    out = await level(setup(FakeAPI(no_time)), place="Przemyśl")
    assert out["level"] == "no_data" and "the state carries no time" in out["no_data_reasons"]
    odd = a_state(cells={PRZEMYSL_CELL: {"level": "severe", "drivers": []}})
    out = await level(setup(FakeAPI(odd)), place="Przemyśl")
    assert out["level"] == "no_data"
    assert "the state gives this place a level this version does not know" in out["no_data_reasons"]


async def test_a_document_of_the_wrong_shape_is_no_data_and_keeps_the_notice():
    bad = a_state(cells={PRZEMYSL_CELL: {"level": "near", "drivers": {"code": "object_near"}}})
    out = await level(setup(FakeAPI(bad)), place="Przemyśl")
    assert out["level"] == "no_data" and out["notice"] == server.TEXTS["notice"]
    assert out["pass_through"]["neptun_link_text"] == NEPTUN_TEXT


async def test_without_the_grid_the_reason_is_the_grid_alone():
    out = await level(setup(FakeAPI(fail={server.GRID_URL})), place="Przemyśl")
    assert out["no_data_reasons"] == ["SanAlert's grid could not be read, so the place's square is not known"]


async def test_english_names_for_towns_on_the_list():
    for name, town in (("Warsaw", "Warszawa"), ("Cracow", "Kraków"), ("krakow", "Kraków"), ("LODZ", "Łódź")):
        assert (await level(setup(FakeAPI()), place=name))["place"]["name"] == town


def test_registry_metadata_matches_the_package():
    """The MCP Registry checks that the PyPI README names the server (`mcp-name:`), and server.json
    must carry the package's own version, or a release publishes metadata for the wrong build."""
    import json
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    meta = json.loads((root / "server.json").read_text())
    marker = re.search(r"mcp-name:\s*(\S+?)\s*-->", (root / "README.md").read_text())
    assert marker and marker[1] == meta["name"] == "io.github.plrojek/sanalert-mcp"
    assert meta["version"] == __version__
    assert [(p["registryType"], p["identifier"], p["version"]) for p in meta["packages"]] == [("pypi", "sanalert-mcp", __version__)]
    assert len(meta["description"]) <= 100
