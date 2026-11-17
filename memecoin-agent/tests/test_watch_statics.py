from datetime import datetime, timedelta, timezone

import httpx
import pytest

from memecoin_agent.config import Config, WatchConfig
from memecoin_agent.watch.alerts import AlertSink
from memecoin_agent.watch.statics import GitHub, StaticsPrWatcher, added_addresses

REPO = "BitGo/BitGoJS"
CASHCAT = "0x020bfc650a365f8bb26819deaabf3e21291018b4"
OTHER = "0x39dbed3a2bd333467115de45665cc57f813c4571"
NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
COINS_PATCH = "@@ -1,2 +1,4 @@\n export const a = 1;\n+  '0x020bfc650a365f8bb26819deaabf3e21291018b4',\n+  'hoodeth:cashcat',\n"
BASE_PATCH = "@@ -1 +1,2 @@\n+  const chain = '0x020bfc650a365f8bb26819deaabf3e21291018b4';\n"


def pr(number, sha, updated=None, state="open"):
    updated = updated or (NOW - timedelta(hours=1))
    return {"number": number, "state": state, "created_at": "2026-07-27T09:00:00Z",
            "updated_at": updated.strftime("%Y-%m-%dT%H:%M:%SZ"), "html_url": f"https://github.com/{REPO}/pull/{number}",
            "head": {"sha": sha}}


class Api:
    def __init__(self):
        self.prs = []
        self.files = {}
        self.etag = '"v1"'
        self.status = None
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status:
            return httpx.Response(self.status, json={"message": "limit"})
        if request.url.path == f"/repos/{REPO}/pulls":
            if request.headers.get("If-None-Match") == self.etag:
                return httpx.Response(304)
            return httpx.Response(200, json=self.prs, headers={"ETag": self.etag})
        number = int(request.url.path.split("/")[-2])
        return httpx.Response(200, json=self.files.get(number, []))


def make(tmp_path, api, recent_hours=48.0):
    gh = GitHub(transport=httpx.MockTransport(api.handler))
    return StaticsPrWatcher(gh, REPO, AlertSink(tmp_path / "alerts.csv"), tmp_path / "statics.json",
                            recent_hours=recent_hours, clock=lambda: NOW)


def test_added_addresses_only_reads_added_lines():
    got = added_addresses(COINS_PATCH + "-  '0x39dbed3a2bd333467115de45665cc57f813c4571',\n")
    assert got == [(CASHCAT, "'0x020bfc650a365f8bb26819deaabf3e21291018b4',")]


def test_alerts_once_for_an_added_address_in_coin_files(tmp_path):
    api = Api()
    api.prs = [pr(9357, "aaa")]
    api.files = {9357: [{"filename": "modules/statics/src/coins/hoodethTokens.ts", "patch": COINS_PATCH}]}
    w = make(tmp_path, api)
    alerts = w.poll_once()
    assert len(alerts) == 1
    assert alerts[0].subject == CASHCAT and "PR #9357" in alerts[0].detail
    assert alerts[0].url.endswith("/pull/9357")


def test_ignores_files_outside_the_coin_path(tmp_path):
    api = Api()
    api.prs = [pr(9357, "aaa")]
    api.files = {9357: [{"filename": "modules/statics/src/base.ts", "patch": BASE_PATCH}]}
    assert make(tmp_path, api).poll_once() == []


def test_same_address_in_same_pr_is_not_repeated_after_a_new_commit(tmp_path):
    api = Api()
    api.prs = [pr(9357, "aaa")]
    api.files = {9357: [{"filename": "modules/statics/src/coins/x.ts", "patch": COINS_PATCH}]}
    w = make(tmp_path, api)
    assert len(w.poll_once()) == 1
    api.prs = [pr(9357, "bbb")]
    api.etag = '"v2"'
    assert w.poll_once() == []


def test_restart_does_not_repeat(tmp_path):
    api = Api()
    api.prs = [pr(9357, "aaa")]
    api.files = {9357: [{"filename": "modules/statics/src/coins/x.ts", "patch": COINS_PATCH}]}
    make(tmp_path, api).poll_once()
    assert make(tmp_path, api).poll_once() == []


def test_old_prs_are_ignored(tmp_path):
    api = Api()
    api.prs = [pr(9357, "aaa", updated=NOW - timedelta(days=5))]
    api.files = {9357: [{"filename": "modules/statics/src/coins/x.ts", "patch": COINS_PATCH}]}
    assert make(tmp_path, api).poll_once() == []


def test_etag_304_skips_the_poll(tmp_path):
    api = Api()
    api.prs = [pr(9357, "aaa")]
    api.files = {9357: [{"filename": "modules/statics/src/coins/x.ts", "patch": COINS_PATCH}]}
    w = make(tmp_path, api)
    w.poll_once()
    api.requests.clear()
    assert w.poll_once() == []
    assert [r.url.path for r in api.requests] == [f"/repos/{REPO}/pulls"]


def test_rate_limit_error_keeps_state_and_does_not_raise(tmp_path):
    api = Api()
    api.status = 403
    w = make(tmp_path, api)
    assert w.poll_once() == []
    assert not (tmp_path / "statics.json").exists() or '"seen": []' in (tmp_path / "statics.json").read_text(encoding="utf-8")


def test_token_is_sent_but_never_in_errors():
    def boom(request):
        raise httpx.ConnectError("no route")

    gh = GitHub(token="ghp_SECRET", transport=httpx.MockTransport(boom))
    with pytest.raises(RuntimeError) as exc:
        gh.get("/repos/x/y/pulls")
    assert "ghp_SECRET" not in str(exc.value)


def test_config_validates_statics_entries():
    with pytest.raises(ValueError):
        Config(watch=WatchConfig(statics_repos=[{"repo": REPO, "bogus": 1}])).validate()
    with pytest.raises(ValueError):
        Config(watch=WatchConfig(statics_interval_s=5)).validate()
