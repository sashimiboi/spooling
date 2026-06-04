"""Tests for spooling.pricing — model rate lookup and cost calculation."""

import pytest
from unittest.mock import patch

from spooling.pricing import (
    ModelRates,
    normalize_model,
    _candidate_keys,
    get_rates,
    PROVIDER_DEFAULT_MODEL,
)
from spooling.config import DEFAULT_PRICING


# ---------------------------------------------------------------------------
# ModelRates.cost
# ---------------------------------------------------------------------------

class TestModelRatesCost:
    def test_basic_cost(self):
        rates = ModelRates(input=3e-6, output=15e-6, cache_write=3.75e-6, cache_read=0.3e-6)
        cost = rates.cost(input_tokens=1_000_000, output_tokens=1_000_000)
        assert abs(cost - 18.0) < 1e-4

    def test_zero_tokens(self):
        rates = ModelRates(input=3e-6, output=15e-6, cache_write=3.75e-6, cache_read=0.3e-6)
        assert rates.cost() == 0.0

    def test_cache_components(self):
        rates = ModelRates(input=3e-6, output=15e-6, cache_write=3.75e-6, cache_read=0.3e-6)
        cost = rates.cost(cache_write_tokens=1_000_000, cache_read_tokens=1_000_000)
        assert abs(cost - 4.05) < 1e-4

    def test_rounding_to_six_places(self):
        rates = ModelRates(input=1e-6, output=1e-6, cache_write=0.0, cache_read=0.0)
        cost = rates.cost(input_tokens=1, output_tokens=1)
        # 2e-6 rounds to 6 decimal places
        assert cost == round(2e-6, 6)


# ---------------------------------------------------------------------------
# normalize_model
# ---------------------------------------------------------------------------

class TestNormalizeModel:
    def test_known_model_unchanged(self):
        assert normalize_model("gpt-4o") == "gpt-4o"

    def test_none_returns_provider_default(self):
        assert normalize_model(None, "copilot") == PROVIDER_DEFAULT_MODEL["copilot"]

    def test_empty_string_returns_provider_default(self):
        assert normalize_model("", "copilot") == PROVIDER_DEFAULT_MODEL["copilot"]

    def test_auto_returns_provider_default(self):
        assert normalize_model("auto", "kiro") == PROVIDER_DEFAULT_MODEL["kiro"]

    def test_synthetic_returns_provider_default(self):
        assert normalize_model("<synthetic>", "cursor") == PROVIDER_DEFAULT_MODEL["cursor"]

    def test_unknown_provider_with_empty_model(self):
        # No provider match — returns None
        result = normalize_model(None, "unknown-provider")
        assert result is None

    def test_whitespace_model_treated_as_empty(self):
        result = normalize_model("   ", "cursor")
        assert result == PROVIDER_DEFAULT_MODEL["cursor"]


# ---------------------------------------------------------------------------
# _candidate_keys
# ---------------------------------------------------------------------------

class TestCandidateKeys:
    def test_plain_model_included(self):
        keys = _candidate_keys("gpt-4o")
        assert "gpt-4o" in keys

    def test_openai_prefix_included(self):
        keys = _candidate_keys("gpt-4o")
        assert "openai/gpt-4o" in keys or "anthropic/gpt-4o" in keys

    def test_bedrock_model_normalized(self):
        keys = _candidate_keys("eu.mistral.mistral-large-2407-v1:0")
        assert any("mistral-large-2407" in k for k in keys)

    def test_gemini_chat_prefix_stripped(self):
        keys = _candidate_keys("chat-gemini-3-0-flash-preview-free-tier")
        assert any("gemini" in k for k in keys)

    def test_empty_model_returns_empty(self):
        assert _candidate_keys("") == []

    def test_no_duplicates(self):
        keys = _candidate_keys("gpt-4o")
        assert len(keys) == len(set(keys))


# ---------------------------------------------------------------------------
# get_rates — uses a fake pricing table to avoid network calls
# ---------------------------------------------------------------------------

FAKE_TABLE = {
    "gpt-4o": {
        "input_cost_per_token": 2.5e-6,
        "output_cost_per_token": 10e-6,
    },
    "gpt-5": {
        "input_cost_per_token": 5e-6,
        "output_cost_per_token": 20e-6,
        "cache_creation_input_token_cost": 6.25e-6,
        "cache_read_input_token_cost": 0.50e-6,
    },
}


@patch("spooling.pricing._get_table", return_value=FAKE_TABLE)
class TestGetRates:
    def test_unknown_model_falls_back(self, _mock):
        rates = get_rates("unknown-model-v1")
        assert rates.input == 3e-6
        assert rates.output == 15e-6

    def test_cache_fields_populated(self, _mock):
        rates = get_rates("gpt-5")
        assert rates.cache_write == 6.25e-6
        assert rates.cache_read == 0.50e-6

    def test_missing_cache_fields_default_to_derived(self, _mock):
        # gpt-4o has no cache fields in our fake table
        rates = get_rates("gpt-4o")
        assert rates.cache_write == pytest.approx(2.5e-6 * 1.25)
        assert rates.cache_read == pytest.approx(2.5e-6 * 0.10)

    def test_unknown_model_falls_back_to_default(self, _mock):
        rates = get_rates("totally-unknown-model-xyz")
        in_per_m, out_per_m = DEFAULT_PRICING
        assert rates.input == pytest.approx(in_per_m / 1_000_000)
        assert rates.output == pytest.approx(out_per_m / 1_000_000)

    def test_none_model_with_provider_uses_default_model(self, _mock):
        # cursor provider default is gpt-4o
        rates = get_rates(None, provider_id="cursor")
        assert rates.input == 2.5e-6

    def test_auto_model_with_provider_resolves(self, _mock):
        rates = get_rates("auto", provider_id="copilot")
        # copilot defaults to gpt-4o
        assert rates.input == 2.5e-6

    def test_cost_calculation_round_trip(self, _mock):
        rates = get_rates("gpt-5")
        cost = rates.cost(input_tokens=1_000, output_tokens=500)
        expected = 1_000 * 5e-6 + 500 * 20e-6
        assert abs(cost - expected) < 1e-9
