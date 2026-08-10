from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from dfri.ingest.ffiec import (
    FDIC_FINANCIALS_URL,
    FFIEC_ITEM_IDENTIFIERS,
    FfiecClient,
    FfiecContractError,
    parse_ffiec_financials,
)
from dfri.ingest.http import HttpReceipt


def _payload() -> bytes:
    rows = [
        {
            "REPDTE": "20260331",
            "CERT": 1,
            "LNCON": 1000,
            "LNCRCD": 300,
            "LNAUTO": 400,
            "LNCONOTH": 200,
            "LNCONORP": 600,
        },
        {
            "REPDTE": "20260331",
            "CERT": 2,
            "LNCON": 500,
            "LNCRCD": 100,
            "LNAUTO": 200,
            "LNCONOTH": 150,
            "LNCONORP": 300,
        },
    ]
    return json.dumps({"meta": {"total": 2}, "data": [{"data": row} for row in rows]}).encode()


def test_ffiec_contract_pins_current_schedule_rc_c_item_6_identifiers() -> None:
    assert FFIEC_ITEM_IDENTIFIERS == {
        "credit_cards": "RCONB538",
        "other_revolving": "RCONB539",
        "automobile": "RCONK137",
        "other_consumer": "RCONK207",
    }


def test_ffiec_parser_reconciles_all_item_6_components() -> None:
    observation = parse_ffiec_financials(
        _payload(),
        report_date=date(2026, 3, 31),
        source_url="https://api.fdic.gov/banks/financials",
        checksum="a" * 64,
    )

    assert observation.record_count == 2
    assert observation.automobile_thousands == 600
    assert observation.other_consumer_thousands == 350
    assert observation.credit_cards_thousands == 400
    assert observation.other_revolving_thousands == 150
    assert observation.automobile_share_of_direct_nonrevolving == pytest.approx(600 / 950)


def test_ffiec_parser_fails_closed_on_normalized_field_drift() -> None:
    payload = json.loads(_payload())
    payload["data"][0]["data"]["LNCONORP"] = 601

    with pytest.raises(FfiecContractError, match="reconcile"):
        parse_ffiec_financials(
            json.dumps(payload).encode(),
            report_date=date(2026, 3, 31),
            source_url="https://api.fdic.gov/banks/financials",
            checksum="a" * 64,
        )


def test_ffiec_client_requests_the_complete_pinned_quarter() -> None:
    class FakeTransport:
        def get(self, url: str, **kwargs: object) -> HttpReceipt:
            assert url == FDIC_FINANCIALS_URL
            assert kwargs["params"] == {
                "filters": "REPDTE:20260331",
                "fields": "REPDTE,CERT,LNCON,LNCRCD,LNAUTO,LNCONOTH,LNCONORP",
                "limit": 10000,
                "format": "json",
            }
            return HttpReceipt(
                content=_payload(),
                source_url="https://api.fdic.gov/banks/financials",
                checksum="a" * 64,
                retrieved_at=datetime(2026, 8, 9, tzinfo=UTC),
                status_code=200,
            )

    observation = FfiecClient(FakeTransport()).fetch(date(2026, 3, 31))  # type: ignore[arg-type]

    assert observation.report_date == date(2026, 3, 31)
    assert observation.consumer_total_thousands == 1500


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda _payload: b"not-json", "valid JSON"),
        (lambda _payload: json.dumps([]).encode(), "must be an object"),
        (lambda _payload: json.dumps({"meta": {}, "data": []}).encode(), "metadata or rows"),
        (
            lambda payload: _mutate(payload, ("meta", "total"), 3),
            "response is incomplete",
        ),
        (lambda payload: _mutate(payload, ("data", 0), {}), "row wrapper changed"),
        (
            lambda payload: _mutate(payload, ("data", 0, "data", "REPDTE"), "20260330"),
            "different report date",
        ),
        (
            lambda payload: _mutate(payload, ("data", 1, "data", "CERT"), 1),
            "duplicates certificate",
        ),
        (
            lambda payload: _mutate(payload, ("data", 0, "data", "LNCON"), -1),
            "nonnegative integer",
        ),
        (
            lambda payload: _mutate(payload, ("data", 0, "data", "LNCON"), "missing"),
            "missing or nonnumeric",
        ),
    ],
)
def test_ffiec_parser_rejects_contract_drift(mutation: object, message: str) -> None:
    content = mutation(_payload())  # type: ignore[operator]

    with pytest.raises(FfiecContractError, match=message):
        parse_ffiec_financials(
            content,
            report_date=date(2026, 3, 31),
            source_url="https://api.fdic.gov/banks/financials",
            checksum="a" * 64,
        )


def _mutate(content: bytes, path: tuple[object, ...], value: object) -> bytes:
    payload = json.loads(content)
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return json.dumps(payload).encode()
