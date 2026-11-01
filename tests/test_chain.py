"""Model-fallback behaviour, driven by a scripted fake client (no network, no real sleeping)."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from invoice_pipeline.llm.chain import ModelChain
from invoice_pipeline.llm.types import (
    AllModelsUnavailable, ExtractionFailed, FatalLLMError, NoUsableModelsError, TextPart,
)
from invoice_pipeline.profiles.india_gst.schema import ExtractionResult

from fakes import (
    FakeLLM, auth_error, bad_request_error, daily_quota_error, not_found_error,
    rate_limit_error, sample_json, server_error,
)

PARTS = [TextPart("page")]


@pytest.fixture
def chain_env(ex_settings):
    sleeps = []
    def make(llm, **overrides):
        s = replace(ex_settings, **overrides) if overrides else ex_settings
        return ModelChain(llm, s, sleep=sleeps.append, rng=lambda: 0.5,
                          now=lambda: datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc))
    return make, sleeps


def run(chain):
    return chain.extract("prompt", PARTS, ExtractionResult)


def test_first_model_wins(chain_env):
    make, _ = chain_env
    llm = FakeLLM({"*": [sample_json()]})
    res = run(make(llm))
    assert res.model == "model-a" and len(res.parsed.invoices) == 2
    assert llm.models_called() == ["model-a"]
    assert (res.input_tokens, res.output_tokens) == (1000, 400)


def test_invalid_json_is_reprompted_with_the_error(chain_env):
    make, _ = chain_env
    llm = FakeLLM({"model-a": ['{"invoices": [{"grand_total": "lots"}]}', sample_json()]})
    res = run(make(llm))
    assert res.model == "model-a" and res.attempts == 2
    retry_parts = llm.calls[1][1]
    assert any(isinstance(p, TextPart) and "rejected" in p.text for p in retry_parts)


def test_invalid_twice_falls_to_next_model(chain_env):
    make, _ = chain_env
    llm = FakeLLM({"model-a": ["not json"], "model-b": [sample_json()]})
    res = run(make(llm))
    assert res.model == "model-b"
    assert llm.models_called() == ["model-a", "model-a", "model-b"]


def test_all_invalid_raises_extraction_failed_with_raw_output(chain_env):
    make, _ = chain_env
    llm = FakeLLM({"*": ["still not json"]})
    with pytest.raises(ExtractionFailed) as e:
        run(make(llm))
    assert e.value.raw_response == "still not json"


def test_empty_response_counts_as_invalid(chain_env):
    make, _ = chain_env
    llm = FakeLLM({"model-a": ["", sample_json()]})
    assert run(make(llm)).attempts == 2


def test_retired_model_is_skipped_for_the_rest_of_the_run(chain_env):
    make, _ = chain_env
    llm = FakeLLM({"model-a": [not_found_error()], "model-b": [sample_json()]})
    chain = make(llm)
    assert run(chain).model == "model-b"
    run(chain)
    assert llm.models_called() == ["model-a", "model-b", "model-b"]  # A tried once only
    assert "model-a" in chain.dead


def test_daily_quota_switches_model_then_defers(chain_env):
    make, _ = chain_env
    llm = FakeLLM({"model-a": [daily_quota_error()], "model-b": [daily_quota_error()]})
    chain = make(llm)
    with pytest.raises(AllModelsUnavailable) as e:
        run(chain)
    assert chain.live_models() == []
    ra = e.value.retry_after
    assert ra > datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
    # next attempt is after midnight Pacific (07:00/08:00 UTC next day), not minutes away
    assert ra.day == 26
    with pytest.raises(AllModelsUnavailable):  # later files fail fast, no more API calls
        run(chain)
    assert len(llm.calls) == 2


def test_per_minute_limit_waits_the_server_suggested_time(chain_env):
    make, sleeps = chain_env
    llm = FakeLLM({"model-a": [rate_limit_error(delay=12), sample_json()]})
    assert run(make(llm)).model == "model-a"
    assert sleeps == [13.0]  # server delay + 1s


def test_server_errors_back_off_exponentially_then_skip_model(chain_env):
    make, sleeps = chain_env
    llm = FakeLLM({"model-a": [server_error(503)], "model-b": [sample_json()]})
    res = run(make(llm, backoff_base_s=2.0))
    assert res.model == "model-b"
    assert sleeps == [2.0]       # a fallback is waiting, so only 1 retry (jitter fixed at 1.0x)
    assert llm.models_called().count("model-a") == 2


def test_last_resort_model_gets_the_full_retry_budget(chain_env):
    make, sleeps = chain_env
    llm = FakeLLM({"model-a": [not_found_error()], "model-b": [server_error(503)]})
    with pytest.raises(AllModelsUnavailable):
        run(make(llm, backoff_base_s=2.0))
    assert llm.models_called().count("model-b") == 4      # 1 try + 3 retries
    assert sleeps == [2.0, 4.0, 8.0]


def test_transient_failures_everywhere_defer_but_models_are_only_resting(chain_env):
    make, _ = chain_env
    llm = FakeLLM({"*": [server_error(503)]})
    chain = make(llm)
    with pytest.raises(AllModelsUnavailable):
        run(chain)
    assert chain.dead == {} and chain.exhausted == {}   # an outage is not a verdict on the model
    assert set(chain.cooling) == {"model-a", "model-b"}


def test_overloaded_model_rests_so_later_files_skip_the_wait(ex_settings):
    clock = [0.0]
    llm = FakeLLM({"model-a": [server_error(503)], "model-b": [sample_json()]})
    chain = ModelChain(llm, replace(ex_settings, model_cooldown_s=600.0), sleep=lambda s: None,
                       monotonic=lambda: clock[0], rng=lambda: 0.5)
    assert run(chain).model == "model-b"
    calls_a = llm.models_called().count("model-a")
    assert calls_a == 2                                   # 1 try + 1 retry (fallback waiting), then it rests
    run(chain)                                            # next file: straight to model-b
    assert llm.models_called().count("model-a") == calls_a
    clock[0] += 601                                       # cooldown over: model-a is tried again
    assert chain.live_models()[0] == "model-a"


def test_bad_request_moves_on(chain_env):
    make, _ = chain_env
    llm = FakeLLM({"model-a": [bad_request_error()], "model-b": [sample_json()]})
    assert run(make(llm)).model == "model-b"


def test_auth_error_is_fatal(chain_env):
    make, _ = chain_env
    with pytest.raises(FatalLLMError):
        run(make(FakeLLM({"*": [auth_error()]})))


def test_unknown_errors_surface_instead_of_being_swallowed(chain_env):
    make, _ = chain_env
    with pytest.raises(ZeroDivisionError):
        run(make(FakeLLM({"*": [ZeroDivisionError("bug")]})))


def test_throttle_spaces_calls(ex_settings):
    sleeps, clock = [], [100.0]
    llm = FakeLLM({"*": [sample_json()]})
    chain = ModelChain(llm, replace(ex_settings, throttle_seconds=6.0),
                       sleep=sleeps.append, monotonic=lambda: clock[0])
    run(chain)
    clock[0] += 2.0          # only 2s passed since the last call
    run(chain)
    assert sleeps == [4.0]


def test_probe_drops_models_the_key_cannot_use(chain_env):
    make, _ = chain_env
    llm = FakeLLM({"*": [sample_json()]}, models=("model-b",))
    chain = make(llm)
    chain.probe()
    assert chain.live_models() == ["model-b"]
    assert run(chain).model == "model-b"
    assert llm.models_called() == ["model-b"]


def test_probe_with_nothing_usable_aborts_with_helpful_message(chain_env):
    make, _ = chain_env
    llm = FakeLLM(models=("gemini-9-flash",))
    with pytest.raises(NoUsableModelsError) as e:
        make(llm).probe()
    assert "gemini-9-flash" in str(e.value)


def test_probe_network_failure_is_not_fatal(chain_env):
    make, _ = chain_env
    llm = FakeLLM()
    llm.list_error = TimeoutError("offline")
    chain = make(llm)
    chain.probe()
    assert chain.live_models() == ["model-a", "model-b"]


def test_probe_auth_failure_is_fatal(chain_env):
    make, _ = chain_env
    llm = FakeLLM()
    llm.list_error = auth_error()
    with pytest.raises(FatalLLMError):
        make(llm).probe()
