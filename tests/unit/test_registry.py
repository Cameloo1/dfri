from __future__ import annotations

from dfri.ingest.registry import (
    load_board_archive_exceptions,
    load_board_series,
    load_board_targets,
    load_census_archive,
    load_context_series,
    load_nyfed_series,
    load_sec_contracts,
    load_source_contracts,
)


def test_board_registry_contains_only_live_board_identifiers() -> None:
    definitions = load_board_series()
    ids = {definition.series_id for definition in definitions}
    assert ids == {
        "DTCTL.M",
        "DTCTLR.M",
        "DTCTLN.M",
        "DTCTL_N.M",
        "DTCTLR_N.M",
        "DTCTLN_N.M",
        "B1029NCBA",
        "B1247NCBA",
        "B3248NCBA",
    }
    assert all("fred.stlouisfed.org" not in definition.license_note for definition in definitions)


def test_board_archive_exceptions_pin_manifest_path_and_declared_dates() -> None:
    exceptions = load_board_archive_exceptions()
    identities = {(item.release, item.manifest_date.isoformat()) for item in exceptions}

    assert identities == {
        ("g19", "2016-02-04"),
        ("h8", "2017-01-12"),
        ("h8", "2020-12-21"),
        ("h8", "2022-11-11"),
    }
    assert all(
        item.evidence_url.startswith("https://www.federalreserve.gov/") for item in exceptions
    )


def test_board_target_registry_pins_release_coherent_board_derivations() -> None:
    definitions = load_board_targets()

    assert {item.target_series_id for item in definitions} == {
        "DELTA_DTCTLR.M",
        "DELTA_DTCTLN.M",
    }
    assert {item.level_series_id for item in definitions} == {"DTCTLR.M", "DTCTLN.M"}
    assert all(item.release == "g19" for item in definitions)
    assert all(item.derived_source == "DFRI_DERIVED_BOARD_FIRST_PRINT_V1" for item in definitions)
    assert all(
        "within the same immutable dated G.19 release" in item.derivation for item in definitions
    )


def test_census_archive_registry_pins_release_coherent_retail_derivation() -> None:
    definition = load_census_archive()

    assert definition.series_id == "DELTA_RETAIL_SALES.M"
    assert definition.lake_source == "CENSUS_MARTS_ARCHIVE"
    assert definition.source_url_pattern.endswith("advYYMM.pdf")
    assert "same immutable dated MARTS Table 1" in definition.derivation
    assert definition.terms_url.startswith("https://www.census.gov/")


def test_every_enabled_source_contract_permits_required_operations() -> None:
    contracts = load_source_contracts()
    assert set(contracts) == {
        "ffiec_call_reports",
        "ncua_call_reports",
        "federal_reserve_board",
        "bea",
        "census",
        "new_york_fed",
        "sec_edgar",
        "treasury_fiscal_data",
        "wikimedia",
    }
    for contract in contracts.values():
        assert contract.automated_access
        assert contract.storage
        if contract.active_publication:
            assert contract.derivative_redistribution
        assert contract.terms_url.startswith("https://")
        assert contract.conditions
    assert contracts["new_york_fed"].status == "retired_incompatible_feed_license"


def test_context_registry_replaces_all_legacy_aliases_directly() -> None:
    definitions = load_context_series()
    aliases = {
        definition.expected_source_attributes["legacy_alias"]
        for definition in definitions
        if "legacy_alias" in definition.expected_source_attributes
    }
    assert aliases == {"PCE", "PCEDG", "PCEND", "PCES", "PI", "RSAFS"}
    assert {definition.source for definition in definitions} == {"bea", "census"}
    assert all("fred.stlouisfed.org" not in definition.license_note for definition in definitions)


def test_nyfed_registry_pins_complete_hhdc_contracts_and_attribution() -> None:
    definitions = load_nyfed_series()

    assert len(definitions) == 21
    assert sum(":BALANCE:" in item.series_id for item in definitions) == 7
    assert sum(":ORIGINATION:" in item.series_id for item in definitions) == 2
    assert sum(":DELINQUENCY:30PLUS:" in item.series_id for item in definitions) == 6
    assert sum(":DELINQUENCY:90PLUS:" in item.series_id for item in definitions) == 6
    for definition in definitions:
        attributes = definition.expected_source_attributes
        assert attributes["source_page"] == (
            "https://www.newyorkfed.org/householdcredit/hhdc-iframe"
        )
        assert attributes["verified_report_period"] == "2026Q1"
        assert attributes["verified_workbook_url"].endswith("/HHD_C_Report_2026Q1")
        assert attributes["sheet"] in {
            "Page 3 Data",
            "Page 6 Data",
            "Page 8 Data",
            "Page 13 Data",
            "Page 14 Data",
        }
        assert attributes["max_lag_quarters"] == "0"
        assert "New York Fed Consumer Credit Panel / Equifax" in definition.license_note
        assert definition.verified_at.isoformat() == "2026-08-04T08:48:52+00:00"


def test_sec_contracts_keep_auto_asset_level_and_card_10d_distinct() -> None:
    contracts = load_sec_contracts()
    reg_ab = contracts["reg_ab_ii"]
    cards = contracts["card_trusts"]
    assert isinstance(reg_ab, dict)
    assert isinstance(cards, dict)
    assert "automobile loans" in reg_ab["asset_level_classes"]
    assert "credit cards" not in reg_ab["asset_level_classes"]
    assert cards["form_url"] == "https://www.sec.gov/files/form10d.pdf"
    assert len(cards["current_evidence"]) == 3
    assert len(cards["metric_exhibits"]) == 3
    assert all("sec.gov/Archives/edgar/data/" in url for url in cards["metric_exhibits"])
    evidence = contracts["auto_abs_ee_evidence"]
    assert isinstance(evidence, list)
    assert len(evidence) == 6
    assert all("sec.gov/Archives/edgar/data/" in url for url in evidence)
