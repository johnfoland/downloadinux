"""Ranking NordVPN servers by load, for `servers` (target design, *Host
application*, "Commands in the container, pinned for P0001-C03").

Moved from the old script unchanged in what it prints. The JSON is fetched
inside the container, which is the thing on the VPN, and ranked here.
"""

from __future__ import annotations

import json

from downloadinux.errors import DownloadinuxError

DEFAULT_CITY = "New_York"
# 228 is the United States. `nordvpn countries` lists names, but the numeric
# ids live in the API.
DEFAULT_COUNTRY = "228"
# limit=0 means "all of them". Anything else returns a proximity-biased
# subset: limit=500 still yielded only 8 US cities and no Chicago.
API = "https://api.nordvpn.com/v1/servers/recommendations?filters[country_id]={country}&limit=0"
UNREACHABLE = "could not reach the Nord API from the container (VPN down, or killswitch blocking?)"
SHOWN = 8
WIDTH = 74


def fetch_argv(country: str = DEFAULT_COUNTRY) -> list[str]:
    """curl, run in the container. `-g`: the brackets in the URL are
    literal, not a curl glob."""
    return ["curl", "-sg", "--max-time", "40", API.format(country=country)]


def rank(body: str, city: str = DEFAULT_CITY) -> list[str]:
    """The lines to print: up to eight servers in `city` (an underscore
    stands for a space), least load first, or, when it has none, the wrapped
    list of cities. A body that is not the server list is an operation
    failure (DEC-18)."""
    want = city.replace("_", " ")
    try:
        servers = [
            (s["locations"][0]["country"]["city"]["name"],
             s["hostname"].replace(".nordvpn.com", ""), s["load"])
            for s in json.loads(body)
        ]
        rows = sorted((s for s in servers if s[0] == want), key=lambda s: s[2])[:SHOWN]
        # Sorted before the underscores go in: a space sorts before capitals.
        cities = [name.replace(" ", "_") for name in sorted({name for name, _, _ in servers})]
    except (ValueError, LookupError, TypeError, AttributeError):
        raise DownloadinuxError(UNREACHABLE) from None

    if rows:
        return ["    %-12s load %s%%" % (host, load) for _, host, load in rows]

    lines = [f"    No servers in {want}. Available cities:"]
    # Wrap the list rather than dumping 54 names down the terminal.
    line = "     "
    for item in cities:
        if len(line) + len(item) > WIDTH:
            lines.append(line)
            line = "     "
        line += " " + item
    if line.strip():
        lines.append(line)
    return lines
