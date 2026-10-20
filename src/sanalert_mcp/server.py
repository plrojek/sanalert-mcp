"""SanAlert's level for one place, for AI assistants: an MCP server over stdio, on the user's computer.

The place never leaves this computer. Like the Home Assistant integration, the server fetches the
same public documents everybody gets (/v1/state, and the grid once per run) and looks the place up
in them with the same rules (core.py, a copy of the integration's, never edited here). A place is
one of the website's towns (towns.json) or coordinates: nothing is geocoded, because a name looked
up online would carry the place out.

Rule 2 of SanAlert's API contract holds everywhere: blind is not calm. No document, one that cannot
be read, one older than 60 s, or a grid that cannot be read is "no_data", never "quiet". The age is
never less than the time this process has held the document (web/src/lib/freshness.ts): a computer
clock that runs behind would otherwise keep an old "quiet" fresh while the API is down. Every
answer carries the state's notice, the attribution and the pass-through of SanAlert's remote MCP
server (api_texts.json, checked against the API by its tests), so whatever an assistant repeats can
carry them on.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
import unicodedata
from collections.abc import Callable
from importlib import resources
from typing import Annotated, Any, Literal

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from . import __version__, core

API = "https://api.sanalert.pl"
STATE_URL = f"{API}/v1/state"
GRID_URL = f"{API}/v1/grid/1.json"
# The API's conditions (llms.txt, rule 6): say which of ours is asking, nothing else about anybody,
# and fetch /v1/state at most once every 5 s, with If-None-Match.
USER_AGENT = f"SanAlert-MCP/{__version__}"
MIN_INTERVAL_S = 5.0
TIMEOUT = httpx.Timeout(10.0)

_PKG = resources.files(__package__)
TEXTS = json.loads(_PKG.joinpath("api_texts.json").read_text(encoding="utf-8"))
TOWNS: list[dict] = json.loads(_PKG.joinpath("towns.json").read_text(encoding="utf-8"))
PASS_THROUGH: dict = TEXTS["pass_through"]

# Whatever a tool is asked is the reader's business: the SDK logs a failed call at INFO with its
# message, so only warnings are let through; httpx's per-request lines go too (stderr, but noise).
logging.getLogger("mcp").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

INSTRUCTIONS = f"""SanAlert (sanalert.pl) is an unofficial, free public service by Applied AI sp. z o.o. (Przemyśl, Poland). It shows whether drones or missiles tracked over Ukraine are heading for places in Poland, as SanAlert levels (quiet, activity, approaching, near), and shows the official Polish RCB and RSO messages separately and unchanged.

This server runs on the reader's own computer. Its one tool, `sanalert_level`, gives SanAlert's level for one place: one of the website's towns, or coordinates. The place stays on this computer: the server fetches the same public documents everybody gets from https://api.sanalert.pl and looks the place up in them. It never sends the place anywhere and never looks a name up online. For the whole of Poland (the state summarised, official messages of the last 24 hours, night reports) SanAlert's remote server is https://api.sanalert.pl/mcp.

{TEXTS["notice"]["en"]}

Whoever shows or repeats this data:
1. Repeats `level_name` and `say` as they are. Never softens, shortens or narrows them, and never calls a place safe: a level says what SanAlert tracks, not that nothing can happen.
2. Keeps the notice visible with it, and a visible link to every `attribution` entry, unchanged.
3. Always refers readers to the official air-raid alarms: sirens, Alert RCB and the instructions of the authorities (NEPTUN's API terms §5).
4. Keeps a visible link to NEPTUN, {PASS_THROUGH["neptun_url"]}, next to the level, for example with the text "{PASS_THROUGH["neptun_link_text"]}" (NEPTUN's API terms §3). NEPTUN's name and logo do not pass with the data (§7).
5. Never turns no data into quiet: when `level` is `no_data`, say "no data", never "quiet" or "calm". A level read more than 60 seconds ago is no data too: call the tool again rather than repeat it.
6. Says that the level is SanAlert's inference, not an official warning. The official warnings are RCB's and RSO's; RCB's classes precaution, heightened and shelter are tiers 1, 2 and 3 of 3, never SanAlert levels.

Every result carries `notice`, `attribution` and `pass_through`: pass them on. The conditions of use in full: https://sanalert.pl/en/terms/ and https://api.sanalert.pl/v1/openapi.json."""

ANSWER_RULES = [
    "Repeat level_name and say as they are: do not soften, shorten or narrow them, and never call the place safe.",
    "no_data is never quiet: say \"no data\". A level read more than 60 seconds ago is no data: call the tool again.",
    "Keep notice visible, link every attribution entry, and link NEPTUN next to the level (pass_through).",
    "Refer the reader to the official air-raid alarms: sirens, Alert RCB and the instructions of the authorities.",
    "The level is SanAlert's inference, not an official warning. RCB tiers 1-3 are not SanAlert levels.",
]
OFFICIAL_NOTE = ("RCB and RSO messages in force for the place's voivodeship: for the voivodeship, never \"for you\". "
                 "null means it cannot be told now (an old document, or RCB's or RSO's messages could not be read): "
                 "never say \"no messages\" then. [] means none is in force. They never change the level.")

# The tool reads public documents from the internet and changes nothing anywhere.
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)


def _key(name: str) -> str:
    """A town's name for matching: case, Polish letters, spaces and hyphens do not matter."""
    folded = unicodedata.normalize("NFKD", name.casefold().replace("ł", "l"))
    return "".join(c for c in folded if c.isalnum() and not unicodedata.combining(c))


TOWN_BY_KEY = {_key(t["name"]): t for t in TOWNS}
# English names an assistant may pass for towns on the list (Kraków, Łódź and the rest fold by themselves).
for _alias, _name in (("Warsaw", "Warszawa"), ("Cracow", "Kraków")):
    if _key(_name) in TOWN_BY_KEY:
        TOWN_BY_KEY[_key(_alias)] = TOWN_BY_KEY[_key(_name)]


def find_town(name: str) -> dict | None:
    return TOWN_BY_KEY.get(_key(name))


def resolve(place: str | None, latitude: float | None, longitude: float | None) -> tuple[str | None, float, float]:
    """The place as (town name or None, latitude, longitude), or an error that never repeats what was asked."""
    if place is not None and place.strip():
        if latitude is not None or longitude is not None:
            raise ToolError("Give either a town (place) or coordinates (latitude and longitude), not both.")
        town = find_town(place)
        if town is None:
            raise ToolError("That is not one of SanAlert's towns: " + ", ".join(t["name"] for t in TOWNS)
                            + ". Give latitude and longitude instead. This server never looks a name up online, "
                              "so the place stays on this computer.")
        return town["name"], float(town["lat"]), float(town["lon"])
    if latitude is None or longitude is None:
        raise ToolError("Give a town (place) or both latitude and longitude.")
    return None, latitude, longitude


class Feed:
    """The two public documents, fetched only when the tool is called: /v1/state at most once every
    MIN_INTERVAL_S with If-None-Match, the grid once per run (again after a failure). A failure is
    kept as a reason and never becomes quiet: an older state stays, and rule 2 ages it into no data."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time) -> None:
        self._transport = transport
        self._clock = clock
        self._wall = wall
        # When this process first saw the state's generated_at, on both clocks: the monotonic one stops
        # while the computer sleeps, the wall one can be set back; the larger time held wins.
        self._seen: tuple[object, float, float] | None = None
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self.state: dict | None = None
        self.state_error: str | None = None
        self._etag: str | None = None
        self._fetched: float | None = None
        self.grid: dict | None = None
        self._grid_tried: float | None = None

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(transport=self._transport, timeout=TIMEOUT, follow_redirects=False,
                                             headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        return self._client

    async def fresh_state(self) -> tuple[dict | None, str | None]:
        async with self._lock:
            now = self._clock()
            if self._fetched is not None and now - self._fetched < MIN_INTERVAL_S:
                return self.state, self.state_error
            self._fetched = now
            headers = {"If-None-Match": self._etag} if self._etag and self.state is not None else {}
            try:
                resp = await self.client().get(STATE_URL, headers=headers)
                if resp.status_code == 304 and self.state is not None:
                    self.state_error = None
                elif resp.status_code == 200:
                    doc = resp.json()
                    if not isinstance(doc, dict):
                        raise ValueError("not a JSON object")
                    if self._seen is None or self._seen[0] != doc.get("generated_at"):
                        self._seen = (doc.get("generated_at"), self._clock(), self._wall())
                    self.state, self._etag, self.state_error = doc, resp.headers.get("ETag"), None
                else:
                    self.state_error = f"SanAlert's API answered HTTP {resp.status_code}"
            except httpx.HTTPError:
                self.state_error = "SanAlert's API could not be reached"
            except ValueError:
                self.state_error = "SanAlert's API sent a document that could not be read"
            return self.state, self.state_error

    def held_s(self) -> float | None:
        """How long this process has held the state it has (None before the first one)."""
        if self._seen is None:
            return None
        return max(self._clock() - self._seen[1], self._wall() - self._seen[2])

    async def voivodeships(self) -> dict[str, str] | None:
        """The grid's cells (cell id -> voivodeship), or None while it cannot be read."""
        async with self._lock:
            if self.grid is None:
                now = self._clock()
                if self._grid_tried is not None and now - self._grid_tried < MIN_INTERVAL_S:
                    return None
                self._grid_tried = now
                try:
                    resp = await self.client().get(GRID_URL)
                    resp.raise_for_status()
                    doc = resp.json()
                    if not isinstance(doc, dict) or not isinstance(doc.get("cells"), dict):
                        raise ValueError("no cells")
                    self.grid = doc
                except (httpx.HTTPError, ValueError):
                    return None
            return self.grid["cells"]


def _no_data_reasons(state: dict | None, state_error: str | None, grid_ok: bool, cell: str, held: float | None,
                     now: dt.datetime) -> list[str]:
    if not grid_ok:                             # the state is not even fetched then
        return ["SanAlert's grid could not be read, so the place's square is not known"]
    reasons = [state_error] if state_error else []
    if not state:
        return reasons or ["no state yet"]
    if held is not None and held > core.MAX_AGE_S:
        reasons.append("no new state for more than 60 seconds")
    if state.get("levels_valid") is not True:
        reasons.append("SanAlert's levels are not valid right now")
    if (state.get("health") or {}).get("status") == "unknown":
        reasons.append("SanAlert's sources are down")
    age = core.age_s(state, now)
    if age is None:
        reasons.append("the state carries no time")
    elif age > core.MAX_AGE_S:
        reasons.append("the state is older than 60 seconds")
    if ((state.get("cells") or {}).get(cell) or {}).get("level", "quiet") not in core.LEVELS:
        reasons.append("the state gives this place a level this version does not know")
    return reasons


def answer(state: dict | None, state_error: str | None, cells: dict[str, str] | None, town: str | None,
           lat: float, lon: float, language: str, now: dt.datetime, held: float | None = None) -> dict[str, Any]:
    """The tool's result for one place: the integration's summary (core.summary), the website's sentence
    as `say`, whether the data can be trusted, and what must stay with it. `held` is how long this
    process has held the state: past 60 s the rules see no state at all, whatever the clock says."""
    cell = core.cell_id(lat, lon)
    grid_ok = cells is not None
    voivodeship = cells.get(cell) if grid_ok else None
    current = None if held is not None and held > core.MAX_AGE_S else state
    if grid_ok:
        s = core.summary(current, cell, voivodeship, now, language)
    else:
        # Without the grid it is not known whether the point is in Poland: no data, never quiet and
        # never "out of range". A voivodeship placeholder keeps the rules on Poland's side.
        s = core.summary(None, cell, "unknown", now, language)
        s.update(voivodeship=None, official=None)
    blind = (not grid_ok) or core.level(current, cell, True, now) == "no_data"
    data = "no_data" if blind else ("delayed" if core.degraded(current) else "ok")
    if s["level"] == "out_of_range":
        s["official"] = None
    out = {
        "notice": (state or {}).get("notice") or TEXTS["notice"],
        "attribution": (state or {}).get("attribution") or TEXTS["attribution"],
        "pass_through": PASS_THROUGH,
        "place": {"name": town, "latitude": lat, "longitude": lon, "cell": cell, "voivodeship": voivodeship},
        "level": s["level"],
        "level_name": s["level_name"],
        "say": s["reason"],
        "data": data,
        "no_data_reasons": _no_data_reasons(state, state_error, grid_ok, cell, held, now) if blind else [],
    }
    for k in ("rank", "reason_code", "kind", "km", "eta_min", "course_known", "lost_at", "oblast", "raion", "threat",
              "risk", "confidence", "degraded", "data_age_s", "generated_at"):
        out[k] = s[k]
    out["official"] = s["official"]
    out["official_note"] = OFFICIAL_NOTE
    out["answer_rules"] = ANSWER_RULES
    return out


def build(feed: Feed | None = None,
          now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc)) -> MCPServer:
    feed = feed or Feed()
    mcp = MCPServer("SanAlert (local)", title="SanAlert for AI assistants (unofficial)", version=__version__,
                    instructions=INSTRUCTIONS, website_url="https://sanalert.pl/en/")

    @mcp.tool(annotations=READ_ONLY)
    async def sanalert_level(
        place: Annotated[str | None, Field(description="One of SanAlert's towns, e.g. Przemyśl, Lublin, Rzeszów or Chełm "
                                                       "(Polish letters optional). Leave it out when you give coordinates.")] = None,
        latitude: Annotated[float | None, Field(ge=-90, le=90, description="The place's latitude, in degrees.")] = None,
        longitude: Annotated[float | None, Field(ge=-180, le=180, description="The place's longitude, in degrees.")] = None,
        language: Annotated[Literal["pl", "en", "uk"], Field(description="The language of level_name and say.")] = "en",
    ) -> dict[str, Any]:
        """SanAlert's level right now for one place in Poland, worked out on this computer: quiet, activity,
        approaching or near, the website's sentence about it (`say`), what raised it, the official RCB and RSO
        messages in force for the place's voivodeship, and whether the data can be trusted now (`data`: ok,
        delayed or no_data; no_data is never quiet). Give one of SanAlert's towns or the coordinates. The place
        is never sent anywhere: the server fetches the same public documents everybody gets."""
        town, lat, lon = resolve(place, latitude, longitude)
        cells = await feed.voivodeships()
        # Without the grid the answer is no data whatever the state says, so the state is not fetched.
        state, state_error = (await feed.fresh_state()) if cells is not None else (None, None)
        at = now()
        try:
            return answer(state, state_error, cells, town, lat, lon, language, at, feed.held_s())
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            # Valid JSON of the wrong shape: no data, with everything that must stay with it.
            return answer(None, "SanAlert's API sent a document that could not be read", cells, town, lat, lon,
                          language, at)

    return mcp


def main() -> None:
    build().run()           # stdio


if __name__ == "__main__":
    main()
