#!/usr/bin/env python3
"""Rank NordVPN servers in a city by load.

Reads Nord's recommendations JSON on stdin (fetched from inside the container,
which is the thing actually on the VPN) and prints the least-loaded servers.

Usage:  ... | servers.py <City_Name>
"""
import json
import sys

want = (sys.argv[1] if len(sys.argv) > 1 else "New_York").replace("_", " ")

try:
    data = json.load(sys.stdin)
except Exception:
    print("    Could not reach the Nord API (VPN down, or killswitch blocking?)")
    sys.exit(0)

rows = sorted(
    (s for s in data if s["locations"][0]["country"]["city"]["name"] == want),
    key=lambda s: s["load"],
)[:8]

if not rows:
    cities = sorted({s["locations"][0]["country"]["city"]["name"] for s in data})
    print("    No servers in %s. Available cities:" % want)
    # Wrap the list rather than dumping 54 names down the terminal.
    line = "     "
    for c in cities:
        item = c.replace(" ", "_")
        if len(line) + len(item) > 74:
            print(line)
            line = "     "
        line += " " + item
    if line.strip():
        print(line)
    sys.exit(0)

for s in rows:
    print("    %-12s load %s%%" % (s["hostname"].replace(".nordvpn.com", ""), s["load"]))
