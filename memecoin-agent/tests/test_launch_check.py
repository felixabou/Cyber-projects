import pytest
from memecoin_agent.watch.launch_check import (
    DEAD,
    EIP7702_PREFIX,
    TRANSFER,
    ZERO,
    analyze,
    check_token,
    scan_transfers,
    shared_recipients,
)

TOKEN = "0x020bfc650a365f8bb26819deaabf3e21291018b4"
TOKEN2 = "0x39dbed3a2bd333467115de45665cc57f813c4571"
POOL = "0xa70fc67c9f69da90b63a0e4c05d229954574e313"
TX = "0x" + "11" * 32


def topic_addr(a):
    return "0x" + "0" * 24 + a[2:].lower()


def log(block, idx, sender, recipient, amount, token=TOKEN):
    return {"address": token, "blockNumber": hex(block), "logIndex": hex(idx), "topics": [
        TRANSFER, topic_addr(sender), topic_addr(recipient)], "data": "0x" + format(amount, "064x")}


class FakeChain:
    def __init__(self, logs, head=1000, creation=100, code=None):
        self.logs, self.head, self.creation = logs, head, creation
        self.code = code or {}
        self.get_logs_calls = 0

    def request(self, method, params):
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getTransactionReceipt":
            return {"blockNumber": hex(self.creation)}
        if method == "eth_getBlockByNumber":
            return {"timestamp": hex(1_000_000 + int(params[0], 16) * 10)}
        if method == "eth_getLogs":
            self.get_logs_calls += 1
            lo, hi = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
            return [lg for lg in self.logs if lo <= int(lg["blockNumber"], 16) <= hi]
        if method == "eth_getCode":
            return self.code.get(params[0].lower(), "0x")
        raise AssertionError(method)


def cashcat_like():
    logs = [log(100, 0, ZERO, POOL, 1_000_000_000), log(101, 0, POOL, "0xaaaa000000000000000000000000000000000001", 20_000_000)]
    for i in range(2, 8):
        logs.append(log(101 + i, 0, POOL, f"0x{i:040x}", 20_000_000))
    logs.append(log(120, 0, POOL, DEAD, 5))
    return logs


def test_reads_transfers_and_finds_identical_buys():
    chain = FakeChain(cashcat_like())
    r = check_token(chain, TOKEN, TX)
    assert r.creation_block == 100
    assert r.transfers_scanned == 9
    ident = [g for g in r.identical if g["amount_raw"] == str(20_000_000)]
    assert ident and ident[0]["wallets"] == 7


def test_mint_and_burn_are_not_recipients():
    r = analyze(FakeChain([]), TOKEN, 100, scan_transfers(FakeChain(cashcat_like()), TOKEN, 100, 50))
    assert ZERO not in r.early_recipients and DEAD not in r.early_recipients


def test_smart_account_share_uses_eip7702_code_prefix():
    logs = [log(101, 0, POOL, "0x" + "a" * 40, 5), log(102, 0, POOL, "0x" + "b" * 40, 6)]
    code = {"0x" + "a" * 40: EIP7702_PREFIX + "0" * 46, "0x" + "b" * 40: "0x"}
    r = check_token(FakeChain(logs, code=code), TOKEN, TX)
    assert r.smart_account_share == 0.5


def test_shared_recipient_across_tokens():
    shared_addr = "0x" + "c" * 40
    a = check_token(FakeChain([log(101, 0, POOL, shared_addr, 5)]), TOKEN, TX)
    b = check_token(FakeChain([log(102, 0, POOL, shared_addr, 9, token=TOKEN2)]), TOKEN2, TX)
    assert shared_recipients([a, b]) == {shared_addr: [TOKEN, TOKEN2]}


def test_scan_stops_at_first_n_transfers():
    chain = FakeChain(cashcat_like())
    got = scan_transfers(chain, TOKEN, 100, 3)
    assert len(got) == 3


def test_scan_respects_the_chunk_range():
    chain = FakeChain([log(100 + 5000, 0, POOL, "0x" + "d" * 40, 1)], head=6000)
    got = scan_transfers(chain, TOKEN, 100, 10)
    assert len(got) == 1 and chain.get_logs_calls >= 2


def test_range_refused_by_provider_is_split_until_accepted():
    class Strict(FakeChain):
        def request(self, method, params):
            if method == "eth_getLogs":
                f = params[0]
                if int(f["toBlock"], 16) - int(f["fromBlock"], 16) >= 100:
                    raise RuntimeError("RPC HTTP 400: block range too large")
            return super().request(method, params)

    chain = Strict([log(105, 0, POOL, "0x" + "e" * 40, 3)], head=400, creation=100)
    got = scan_transfers(chain, TOKEN, 100, 10)
    assert [t.amount for t in got] == [3]


def test_provider_refusing_every_range_raises_instead_of_returning_empty():
    class Refuses(FakeChain):
        def request(self, method, params):
            if method == "eth_getLogs":
                raise RuntimeError("RPC HTTP 400: unsupported")
            return super().request(method, params)

    with pytest.raises(RuntimeError, match="unsupported"):
        scan_transfers(Refuses([]), TOKEN, 100, 10)
