import pytest

from aegentra.models import decode_arguments, encode_arguments


@pytest.mark.parametrize("value", [1.5, float("nan"), float("inf"), object(), (1, 2)])
def test_payloads_do_not_stringify_or_round_unsupported_values(value: object) -> None:
    with pytest.raises(TypeError, match="Unsupported argument type"):
        encode_arguments({"value": value})


def test_non_string_keys_are_not_coerced() -> None:
    with pytest.raises(TypeError, match="keys must be strings"):
        encode_arguments({1: "one"})


def test_nested_json_is_deterministic_and_round_trips() -> None:
    payload = {"list": [{"z": 2, "a": None}, True, "text"], "count": 3}
    encoded = encode_arguments(payload)
    assert encoded == '{"count":3,"list":[{"a":null,"z":2},true,"text"]}'
    assert decode_arguments(encoded) == payload
