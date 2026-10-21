# SanAlert for AI assistants

<!-- mcp-name: io.github.plrojek/sanalert-mcp -->

The unofficial threat level from the air for a place in Poland, for an AI assistant that can run a
local MCP server (Claude Code, Claude Desktop and others). Ask it "what is the level in Przemyśl?"
and the assistant gets the answer the website would show for that place. Not an official warning
system: it does not replace sirens, Alert RCB or instructions from the services.

## What it does, and what it sends

- It runs on your computer and talks to the assistant over stdio.
- When the assistant asks about a place, it fetches `https://api.sanalert.pl/v1/state`, the same
  public document the website and the apps read: at most once every 5 s, with `If-None-Match` and
  the User-Agent `SanAlert-MCP/<version>`. Once per run it fetches the public grid
  (`/v1/grid/1.json`) to find the place's ~11 km square and its voivodeship.
- It looks the place's level up in that document with the rules SanAlert's website and apps follow
  (sanalert.pl/jak-to-dziala). **The place stays on your computer**: no request carries it, and a
  town's name is never looked up online.
- The privacy page says the same (sanalert.pl/prywatnosc, "Serwer SanAlert dla asystentów AI").
  [Terms](https://sanalert.pl/en/terms/) ([Regulamin](https://sanalert.pl/regulamin/),
  [Умови](https://sanalert.pl/ua/terms/)).

## The tool

`sanalert_level` takes `place`, one of the website's towns (Przemyśl, Lublin, Rzeszów, Chełm and
the rest of its list; Polish letters optional, and Warsaw and Cracow work too), or `latitude` and `longitude`, and `language`:
`pl`, `en` or `uk`.

| Field | What it is |
|---|---|
| `level` | `quiet`, `activity`, `approaching`, `near`, `no_data` or `out_of_range` |
| `level_name`, `say` | the level's name and the website's sentence under it, in the chosen language |
| `reason_code`, `kind`, `km`, `eta_min`, `course_known`, `lost_at`, `oblast`, `raion`, `threat` | what raised the level, as the website shows it |
| `risk`, `confidence`, `degraded` | as in the state; `degraded` means the data may be delayed |
| `data`, `no_data_reasons`, `data_age_s`, `generated_at` | whether the level can be trusted now, and why not |
| `place` | the town or the point, its square (`cell`) and voivodeship |
| `official` | RCB and RSO messages in force for the voivodeship; `null` while that cannot be told |
| `notice`, `attribution`, `pass_through` | what has to stay with the data wherever it is shown |

**Blind is not calm.** When the document is missing, cannot be read or is older than 60 s, or the
grid cannot be read, the level is `no_data`, never `quiet`, and `say` says so. The age is never less
than the time the server has held the document, so a computer clock that runs behind cannot keep an
old "quiet" fresh while the API is down. A point outside
Poland's grid is `out_of_range`. A name that is not on the list is an error that names the list:
give coordinates instead.

For the whole of Poland (the state summarised, official messages of the last 24 hours, night
reports, the reliability record) SanAlert runs a remote MCP server, `https://api.sanalert.pl/mcp`.
It takes no place.

## Install

It needs [uv](https://docs.astral.sh/uv/).

Claude Code:

```sh
claude mcp add sanalert -- uvx sanalert-mcp
claude mcp add --transport http sanalert-poland https://api.sanalert.pl/mcp      # optional: the whole of Poland
```

Or both at once, as a Claude Code plugin: `/plugin marketplace add plrojek/agent-skills`, then
`/plugin install sanalert@plrojek`. Straight from GitHub instead of PyPI:
`uvx --from git+https://github.com/plrojek/sanalert-mcp sanalert-mcp`.

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "sanalert": {
      "command": "uvx",
      "args": ["sanalert-mcp"]
    }
  }
}
```

## Conditions of use

Whoever shows or repeats the data keeps `notice` visible with it and a visible link to every
`attribution` entry, always refers readers to the official air-raid alarms (sirens, Alert RCB, the
instructions of the authorities), keeps a visible link to NEPTUN (https://neptun.in.ua, for example
"Дані: Карта повітряних тривог — NEPTUN") next to the level, never turns no data into quiet, and
says that the level is SanAlert's inference, not an official warning. In full:
[terms](https://sanalert.pl/en/terms/) and the [API description](https://api.sanalert.pl/v1/openapi.json).

## Development

`uv run pytest` runs the tests; none of them goes online.

Three files in `src/sanalert_mcp/` are generated, never edited by hand:

- `core.py` is the Home Assistant integration's `core.py`, the same rules in the same words;
- `towns.json` is the apps' town list (`ios/SanAlertKit/Sources/SanAlertKit/Resources/towns-v1.json`,
  made from the website's `web/src/lib/cities.ts`);
- `api_texts.json` is the API's notice, attribution and pass-through (`api/sanalert/state.py`,
  `api/sanalert/mcp_server.py`).

`python scripts/sync.py` copies the first two and `python scripts/sync.py --check` fails on any
difference (CI runs it). The API's tests fail when `api_texts.json` no longer matches the API.

The source is `integrations/mcp/` in SanAlert's own repository; the public repository
(github.com/plrojek/sanalert-mcp) is a copy of it, refreshed with each release, so a fix in
`core.py` reaches users with the next release.

## License

MIT, Applied AI sp. z o.o.
