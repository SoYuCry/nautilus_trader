"""Read-only portable environment and required native API check."""

import json
import sys
from pathlib import Path

import collector_native
import numpy
import pyarrow

import nautilus_trader
from nautilus_trader.model import PRECISION_BYTES
from nautilus_trader.model import BinaryOption
from nautilus_trader.model import OrderBook
from nautilus_trader.persistence import ParquetDataCatalog


assert nautilus_trader.__version__ == "2.0.0rc6", nautilus_trader.__version__
assert int(PRECISION_BYTES) == 16, PRECISION_BYTES
for name in (
    "write_instruments",
    "write_order_book_deltas",
    "write_trade_ticks",
    "write_instrument_closes",
    "query_order_book_deltas",
):
    assert hasattr(ParquetDataCatalog, name), name
assert hasattr(OrderBook, "asks_to_dict") and hasattr(BinaryOption, "to_dict")
assert collector_native.PRUNER_PATH == Path(__file__).resolve().parent / "bbo_prune.py"
print(
    json.dumps(
        {
            "python": sys.version,
            "nautilus": nautilus_trader.__version__,
            "precision_bytes": int(PRECISION_BYTES),
            "pyarrow": pyarrow.__version__,
            "numpy": numpy.__version__,
            "status": "passed",
        },
        indent=2,
    )
)
