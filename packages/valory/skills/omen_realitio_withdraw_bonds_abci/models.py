# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2026 Valory AG
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
# ------------------------------------------------------------------------------

"""Shared state and params for the omen_realitio_withdraw_bonds_abci skill."""

from typing import Any, Dict, Type

from aea.exceptions import enforce

from packages.valory.skills.abstract_round_abci.base import AbciApp
from packages.valory.skills.abstract_round_abci.models import (
    ApiSpecs,
    BaseParams,
)
from packages.valory.skills.abstract_round_abci.models import (
    BenchmarkTool as BaseBenchmarkTool,
)
from packages.valory.skills.abstract_round_abci.models import Requests as BaseRequests
from packages.valory.skills.abstract_round_abci.models import (
    SharedState as BaseSharedState,
)
from packages.valory.skills.omen_realitio_withdraw_bonds_abci.rounds import (
    OmenRealitioWithdrawBondsAbciApp,
)


class SharedState(BaseSharedState):
    """Shared state of the skill."""

    abci_app_cls: Type[AbciApp] = OmenRealitioWithdrawBondsAbciApp

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize."""
        super().__init__(*args, **kwargs)
        # Per-question cache of pre-built claimWinnings tx dicts, keyed by
        # the subgraph response question id (composite "{contract}-{qid}").
        # ``RealitioWithdrawBondsBehaviour._try_build_single_claim`` is
        # RPC-heavy (eth_getLogs + eth_call simulation + safe-tx build);
        # caching the result lets a round that times out before settling
        # reuse the build on the next cycle instead of restarting from
        # scratch. Entries are evicted on each cycle when the underlying
        # question no longer appears in the subgraph's claimable set
        # (settled by an earlier successful multisend, or otherwise no
        # longer relevant).
        #
        # Type is ``Dict[str, Dict[str, Any]]`` (not ``Dict[str, Any]``)
        # so that a future contributor cannot silently insert a non-dict
        # sentinel (e.g. ``None`` to mean "permanently skip"); the cache
        # contract is "every value is a fully-formed claim tx dict ready
        # to be passed to the multisend builder".
        self.realitio_claim_build_cache: Dict[str, Dict[str, Any]] = {}


class RealitioWithdrawBondsParams(BaseParams):
    """Parameters for the omen_realitio_withdraw_bonds_abci skill."""

    multisend_address: str
    multisend_batch_size: int

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the parameters object."""
        self.realitio_withdraw_bonds_batch_size = self._ensure(
            "realitio_withdraw_bonds_batch_size", kwargs, type_=int
        )
        self.min_realitio_withdraw_balance = self._ensure(
            "min_realitio_withdraw_balance", kwargs, type_=int
        )
        # Cap on the eth_getLogs window when scanning LogNewAnswer events.
        # Public Gnosis RPCs disagree on the maximum: BlockPI caps at 1000
        # blocks, others accept much wider ranges. Default tuned to 1000
        # so the lowest common denominator works without env overrides.
        self.event_filtering_batch_size = self._ensure(
            "event_filtering_batch_size", kwargs, type_=int
        )
        # Contract address is read without popping so sibling params
        # classes in a composed MRO can still see it.
        self.realitio_contract: str = kwargs.get(
            "realitio_contract"
        )  # type: ignore[assignment]
        enforce(
            self.realitio_contract is not None,
            "`realitio_contract` is required",
        )
        super().__init__(*args, **kwargs)


class RealitioSubgraph(ApiSpecs):
    """ApiSpecs wrapper for the Realitio subgraph."""


Requests = BaseRequests
BenchmarkTool = BaseBenchmarkTool
