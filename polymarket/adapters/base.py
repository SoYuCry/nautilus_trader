"""Temporary ingress adapter protocol for Polymarket v1 datasets.

Adapters are not intended to become a broad compatibility surface.  They exist
to patch current pre-contract inputs into the required PolymarketL2DatasetV1
shape until the data/IT feed can deliver that contract directly.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from polymarket._core.models import PolymarketL2DatasetV1


class DatasetAdapterV1(Protocol):
    """Translate one pre-contract source into the required L2 dataset."""

    adapter_name: str
    adapter_version: str

    def load(self, config: Mapping[str, Any]) -> PolymarketL2DatasetV1:
        """Load pre-contract data into required replay steps."""
        ...

