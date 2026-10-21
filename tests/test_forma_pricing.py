"""The ledger prices calls at Anthropic's list prices (checked 21 Oct 2026)."""
from types import SimpleNamespace

import pytest

from app.core.forma_core import HAIKU, SONNET, cost_cents


def _usage(i=0, o=0, read=0, write=0):
    return SimpleNamespace(
        input_tokens=i, output_tokens=o,
        cache_read_input_tokens=read, cache_creation_input_tokens=write,
    )


@pytest.mark.parametrize("model, usage, cents", [
    (SONNET, _usage(i=1_000_000), 200.0),        # $2 per million in
    (SONNET, _usage(o=1_000_000), 1000.0),       # $10 per million out
    (SONNET, _usage(read=1_000_000), 20.0),      # cache read 0.1x
    (SONNET, _usage(write=1_000_000), 250.0),    # cache write 1.25x
    (HAIKU, _usage(i=1_000_000, o=1_000_000), 600.0),  # dated id, $1 + $5
])
def test_list_prices(model, usage, cents):
    assert cost_cents(model, usage) == pytest.approx(cents)
