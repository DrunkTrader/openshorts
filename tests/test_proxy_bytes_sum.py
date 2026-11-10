"""A range job downloads twice (audio, then pieces): the ledger sums both."""
import io

import pytest

from cloud import proxy_ledger

app = pytest.importorskip("app")


def _route(winner, paid, label):
    return "PROXY_ROUTE=" + __import__("json").dumps(
        {"winner": winner, "paid_bytes": paid, "attempts": [{"label": label, "ok": True}]})


def test_merge_routes_sums_paid_bytes_and_keeps_a_paid_winner():
    audio = {"winner": "HD", "paid_bytes": 5, "attempts": [{"label": "HD"}]}
    pieces = {"winner": "HD-direct", "paid_bytes": 0, "attempts": [{"label": "HD-direct"}]}
    merged = proxy_ledger.merge_routes(audio, pieces)
    assert merged["paid_bytes"] == 5 and merged["winner"] == "HD"
    assert [a["label"] for a in merged["attempts"]] == ["HD", "HD-direct"]
    assert proxy_ledger.used_paid(merged)
    assert proxy_ledger.merge_routes(None, pieces) is pieces


def test_two_download_lines_are_summed_by_the_log_reader():
    app.jobs["t-sum"] = {"logs": []}
    try:
        lines = ["PROXY_ROUTE=" + '{"winner": "HD", "paid_bytes": 100, "attempts": []}',
                 "PROXY_BYTES=100",
                 "PROXY_ROUTE=" + '{"winner": "HD", "paid_bytes": 40, "attempts": []}',
                 "PROXY_BYTES=40"]
        app.enqueue_output(io.BytesIO("".join(l + "\n" for l in lines).encode()), "t-sum")
        assert app.jobs["t-sum"]["proxy_bytes"] == 140
        assert proxy_ledger.paid_bytes_of(app.jobs["t-sum"]["proxy_route"]) == 140
    finally:
        app.jobs.pop("t-sum", None)
