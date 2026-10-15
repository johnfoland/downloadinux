"""Server data and CLI formatting share a ranking and fetch (TUI §7)."""

import json
from dataclasses import FrozenInstanceError

import pytest
from test_container_commands import BODY, FIXTURES, compose_args

from downloadinux import operations, servers
from downloadinux.errors import DownloadinuxError


def test_fixture_ranking_has_stripped_hosts_original_loads_and_all_cities():
    data = json.loads(BODY)
    expected = sorted((s for s in data if s["locations"][0]["country"]["city"]["name"] == "New York"),
                      key=lambda s: s["load"])[:8]
    choices = servers.ranking(BODY, "New_York")
    assert choices.ranked == tuple(servers.ServerLoad(s["hostname"].replace(".nordvpn.com", ""), s["load"])
                                   for s in expected)
    assert len(choices.ranked) == 8
    assert all(".nordvpn.com" not in s.host for s in choices.ranked)
    assert choices.cities == tuple(name.replace(" ", "_") for name in sorted({
        s["locations"][0]["country"]["city"]["name"] for s in data}))
    with pytest.raises(FrozenInstanceError):
        choices.cities = ()
    with pytest.raises(FrozenInstanceError):
        choices.ranked[0].host = "other"
    assert servers.rank(BODY, "New_York") == [
        "    %-12s load %s%%" % (s.host, s.load) for s in choices.ranked]


def test_missing_city_keeps_city_names_in_cli_order():
    choices = servers.ranking(BODY, "Atlantis")
    assert choices.ranked == ()
    expected_lines = (FIXTURES / "atlantis.txt").read_text().splitlines()
    assert choices.cities == tuple(" ".join(expected_lines[1:]).split())
    assert choices.cities == servers.ranking(BODY, "New_York").cities
    assert servers.rank(BODY, "Atlantis") == expected_lines


def test_ties_keep_api_order_and_city_sort_precedes_underscores():
    data = [{"hostname": f"h{i}.nordvpn.com", "load": load,
             "locations": [{"country": {"city": {"name": city}}}]}
            for i, (load, city) in enumerate([
                (30, "New York"), (5, "New York"), (5, "St Louis"), (7, "New York"),
                (5, "New York"), (1, "StA"), *[(40 + i, "New York") for i in range(8)]])]
    body = json.dumps(data)
    choices = servers.ranking(body)
    assert choices.ranked == tuple(servers.ServerLoad(f"h{i}", load) for i, load in [
        (1, 5), (4, 5), (3, 7), (0, 30), (6, 40), (7, 41), (8, 42), (9, 43)])
    assert choices.cities == ("New_York", "St_Louis", "StA")
    assert servers.rank(body) == ["    %-12s load %s%%" % (s.host, s.load) for s in choices.ranked]


@pytest.mark.parametrize("load", ["5", 4.5, None, {"unvalidated": True}])
def test_load_keeps_existing_rank_validation(load):
    body = json.dumps([{"hostname": "h.nordvpn.com", "load": load,
                        "locations": [{"country": {"city": {"name": "New York"}}}]}])
    assert servers.ranking(body).ranked == (servers.ServerLoad("h", load),)
    assert servers.rank(body) == ["    %-12s load %s%%" % ("h", load)]


@pytest.mark.parametrize("body", ["", "<html>blocked</html>", '{"errors":"nope"}',
                                  '[{"hostname":"us1"}]', "[1,2]"])
def test_ranking_refuses_every_existing_unreachable_body(body):
    with pytest.raises(DownloadinuxError) as caught:
        servers.ranking(body)
    assert str(caught.value) == servers.UNREACHABLE


@pytest.mark.parametrize("enabled", ["false", "true"])
@pytest.mark.parametrize("country", ["228", "38"])
def test_choices_uses_identical_fetch_argv_with_vpn_on_or_off(enabled, country):
    seen = []
    def capture(argv, environ):
        seen.append((list(argv), dict(environ)))
        return 0, BODY
    kwargs = dict(country=country, overrides=(f"vpn.enabled={enabled}",), capture=capture)
    assert operations.server_choices(**kwargs) == servers.ranking(BODY)
    assert operations.list_servers(**kwargs) == servers.rank(BODY)
    assert len(seen) == 2 and seen[0] == seen[1]
    assert compose_args(seen[0][0]) == ["exec", "-T", "-u", "dl", "downloadinux", *servers.fetch_argv(country)]
    assert ("compose/vpn.yaml" in " ".join(seen[0][0])) is (enabled == "true")


@pytest.mark.parametrize("status,body", [(28, ""), (7, BODY)])
def test_choices_refuses_nonzero_fetch_even_with_valid_body(status, body):
    with pytest.raises(DownloadinuxError) as caught:
        operations.server_choices(capture=lambda *_: (status, body))
    assert str(caught.value) == servers.UNREACHABLE
