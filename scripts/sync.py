"""Copy the two files this package takes from the rest of the monorepo, unchanged:

- core.py: the Home Assistant integration's rules for one place (rule 2, rule 4, the website's words);
- towns.json: the apps' town list, which ios/scripts/sync-towns.mjs makes from web/src/lib/cities.ts.

    python scripts/sync.py            copy them
    python scripts/sync.py --check    fail when a copy differs (CI)

Never edit the copies: change the source and run this. api_texts.json is checked by the API's tests
instead (api/tests/test_mcp.py), because only the API's environment can import its texts.
"""
import pathlib
import shutil
import sys

HERE = pathlib.Path(__file__).resolve().parent.parent          # integrations/mcp
REPO = HERE.parent.parent
PKG = HERE / "src" / "sanalert_mcp"
COPIES = {
    PKG / "core.py": REPO / "integrations" / "home-assistant" / "custom_components" / "sanalert" / "core.py",
    PKG / "towns.json": REPO / "ios" / "SanAlertKit" / "Sources" / "SanAlertKit" / "Resources" / "towns-v1.json",
}


def main(check: bool) -> int:
    stale = [copy for copy, source in COPIES.items()
             if not copy.exists() or copy.read_bytes() != source.read_bytes()]
    if check:
        for copy in stale:
            print(f"{copy.relative_to(REPO)} differs from {COPIES[copy].relative_to(REPO)}: "
                  "run integrations/mcp/scripts/sync.py", file=sys.stderr)
        return 1 if stale else 0
    for copy in stale:
        shutil.copyfile(COPIES[copy], copy)
        print(f"copied {COPIES[copy].relative_to(REPO)} -> {copy.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main("--check" in sys.argv[1:]))
