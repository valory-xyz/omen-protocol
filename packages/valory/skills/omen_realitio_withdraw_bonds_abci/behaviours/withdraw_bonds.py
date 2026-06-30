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

"""RealitioWithdrawBondsBehaviour for the omen_realitio_withdraw_bonds_abci skill."""

from string import Template
from typing import Any, Dict, Generator, List, Optional, cast

from packages.valory.contracts.realitio.contract import RealitioContract
from packages.valory.protocols.contract_api import ContractApiMessage
from packages.valory.skills.omen_realitio_withdraw_bonds_abci.behaviours.base import (
    ETHER_VALUE,
    RealitioWithdrawBondsBaseBehaviour,
    SKILL_LOG_PREFIX,
    assemble_claim_params,
    get_callable_name,
    wei_to_str,
)
from packages.valory.skills.omen_realitio_withdraw_bonds_abci.payloads import (
    RealitioWithdrawBondsPayload,
)
from packages.valory.skills.omen_realitio_withdraw_bonds_abci.rounds import (
    RealitioWithdrawBondsRound,
)

# The ``historyHash_not`` filter excludes questions already claimed on-chain
# (their history hash is cleared to zero). Without it, a query bounded by
# batch_size would keep returning the same already-claimed questions and
# deadlock the backlog drain.
#
# ``answerFinalizedTimestamp_lt`` bounds the query to questions whose
# finalization timestamp is already in the past. Without it, the query
# returns questions still inside their Realitio timeout window (finalization
# is set but in the future), causing thousands of failed on-chain
# simulations per day ("question must be finalized" revert).
CLAIMABLE_RESPONSES_QUERY = Template("""{
    responses(
      where: {
        user: "$safe",
        question_: {
          answerFinalizedTimestamp_gt: 0,
          answerFinalizedTimestamp_lt: $current_timestamp,
          historyHash_not: "0x0000000000000000000000000000000000000000000000000000000000000000"
        }
      }
      first: $batch_size
      orderBy: id
      orderDirection: desc
    ) {
      id
      question {
        id
        createdBlock
        updatedBlock
      }
      bond
      answer
    }
  }""")

# Safety margin (blocks) added above ``Question.updatedBlock`` when
# bounding the eth_getLogs upper bound. A small margin protects against
# subgraph indexing skew where the final LogNewAnswer could land in a
# block or two after what the subgraph has indexed as ``updatedBlock``.
_TO_BLOCK_SAFETY_MARGIN = 2


class RealitioWithdrawBondsBehaviour(RealitioWithdrawBondsBaseBehaviour):
    """Query the Realitio subgraph and build a multisend of withdraw + claimWinnings txs."""

    matching_round = RealitioWithdrawBondsRound

    def async_act(self) -> Generator:
        """Single-round act: query → build → wrap → settle-or-skip."""
        with self.context.benchmark_tool.measure(self.behaviour_id).local():
            tx_hash = yield from self._prepare_multisend()
            tx_submitter: Optional[str] = (
                self.matching_round.auto_round_id() if tx_hash is not None else None
            )
            payload = RealitioWithdrawBondsPayload(
                sender=self.context.agent_address,
                tx_submitter=tx_submitter,
                tx_hash=tx_hash,
            )
        with self.context.benchmark_tool.measure(self.behaviour_id).consensus():
            yield from self.send_a2a_transaction(payload)
            yield from self.wait_until_round_end()
        self.set_done()

    def _prepare_multisend(self) -> Generator[None, None, Optional[str]]:
        """Build optional withdraw tx + claim txs and wrap in a multisend.

        :yield: contract-api / ledger-api requests during the build.
        :return: multisend tx hash, or ``None`` when nothing to do.
        """
        txs: List[Dict[str, Any]] = []

        # Withdraw first: the O(1) withdraw must not be starved by a slow
        # or partially failing claim loop.
        withdraw_tx = yield from self._maybe_build_withdraw_tx()
        if withdraw_tx is not None:
            txs.append(withdraw_tx)

        claim_txs = yield from self._build_claim_txs()
        txs.extend(claim_txs)

        if not txs:
            self.context.logger.info(
                f"{SKILL_LOG_PREFIX} nothing to withdraw or claim this period"
            )
            return None

        tx_hash = yield from self._to_multisend(txs)
        if tx_hash is None:
            self.context.logger.warning(f"{SKILL_LOG_PREFIX} multisend build failed")
            return None

        n_claims = len(claim_txs)
        has_withdraw = withdraw_tx is not None
        self.context.logger.info(
            f"{SKILL_LOG_PREFIX} prepared multisend: "
            f"withdraw={has_withdraw}, claims={n_claims}"
        )
        return tx_hash

    def _build_claim_txs(self) -> Generator[None, None, List[Dict[str, Any]]]:
        """Query subgraph for finalized responses and build claimWinnings txs."""
        responses = yield from self._get_claimable_responses()
        if not responses:
            return []

        # Fix F from claim_bonds_fix.md: the safe may have posted
        # multiple responses on the same question (defending its answer
        # through several rounds of bond escalation). Each distinct
        # response is a separate subgraph entry but they all share the
        # same question_id, and a single successful claimWinnings call
        # claims ALL of them at once. Deduplicate by question id so we
        # don't build multiple claim txs for the same question — the
        # second one would revert on-chain because the first one clears
        # the history hash.
        seen_question_ids: set = set()
        unique_responses: List[Dict[str, Any]] = []
        for resp in responses:
            qid = resp.get("question", {}).get("id")
            if qid is None or qid in seen_question_ids:
                continue
            seen_question_ids.add(qid)
            unique_responses.append(resp)

        # Resume-after-timeout cache. The whole ``_prepare_multisend``
        # chain runs inline within a single round's ``round_timeout``;
        # if the timeout fires (RPC slow, queue large, transient TM
        # reset shifting the round clock) the payload is discarded and
        # all per-claim build work is wasted. The shared cache lets a
        # subsequent cycle reuse claim txs already built for the same
        # question, so progress is monotonic regardless of timing.
        # Eviction is driven by the subgraph: questions no longer
        # claimable (settled by an earlier successful multisend, or
        # otherwise no longer in the result set) are dropped here so
        # the cache cannot grow unbounded.
        #
        # Stale-cache safety: ``_simulate_claim`` is re-run on every
        # cache hit. TheGraph indexing can lag behind on-chain state,
        # so a question already claimed in a previous multisend may
        # still appear in ``unique_responses`` for a cycle or two and
        # avoid the subgraph-driven eviction above. Without re-
        # validation the cached ``claimWinnings`` calldata would be
        # re-served and would revert on-chain; ``MultiSend.delegatecall``
        # with ``requireSuccess=true`` would then revert the ENTIRE
        # batch, killing every other claim in the same multisend. Re-
        # simulating is one ``eth_call`` per hit -- cheap relative to
        # the full RPC build chain we're caching around -- and turns
        # the rare-but-catastrophic "bad batch" into a "skip this
        # claim, keep the rest" outcome.
        #
        # N>1 consensus safety: this cache lives on the per-agent in-
        # memory ``SharedState``, not on the consensus-replicated
        # ``SynchronizedData``. ``RealitioWithdrawBondsRound`` is a
        # ``CollectSameUntilThresholdRound`` whose ``selection_key``
        # is ``(tx_submitter, most_voted_tx_hash)``; quorum is a
        # configured threshold (typically 2/3), not unanimity, so a
        # single diverging agent does not by itself block consensus.
        # In single-agent services this is trivially safe. In multi-
        # agent deployments the cache AMPLIFIES (but does not
        # introduce) the pre-existing subgraph-replica-divergence
        # risk: cached entries on one agent persist for as long as
        # that agent's replica keeps listing the question, extending
        # any ``NO_MAJORITY`` window from "one cycle" to "until the
        # replica converges". Multi-agent consumers should pin a
        # single subgraph endpoint across agents or move the cache
        # into consensus state.
        # ``self.context.state`` resolves to the COMPOSED skill's
        # ``SharedState``, not this sub-skill's, when the skill is chained
        # into a larger ABCI app (e.g. market_resolver_abci /
        # market_maker_abci). The composed ``SharedState`` does not run
        # this skill's ``SharedState.__init__``, so the cache attribute is
        # absent there and a direct read raises ``AttributeError`` -- which
        # under ``skill_exception_policy: stop_and_exit`` crash-loops the
        # agent. Lazily create the cache on the composed state on first use
        # so the skill is correct for ANY consumer composition without each
        # consumer having to re-declare the field.
        cache: Optional[Dict[str, Dict[str, Any]]] = getattr(
            self.context.state, "realitio_claim_build_cache", None
        )
        if cache is None:
            cache = {}
            self.context.state.realitio_claim_build_cache = cache
        claimable_ids = {resp["question"]["id"] for resp in unique_responses}
        evicted = [qid for qid in cache if qid not in claimable_ids]
        for qid in evicted:
            del cache[qid]

        batch_size = self.params.realitio_withdraw_bonds_batch_size
        txs: List[Dict[str, Any]] = []
        hits = 0
        stale_evictions = 0
        for resp in unique_responses:
            if len(txs) >= batch_size:
                # Defensive: ``unique_responses`` is already bounded
                # by the subgraph query's ``first: $batch_size`` and
                # dedup only shrinks it, so this is unreachable
                # today. Kept as a guard against future decoupling
                # of the loop cap from the subgraph cap.
                break
            qid = resp["question"]["id"]
            entry = cache.get(qid)
            if entry is not None:
                sim_ok = yield from self._simulate_claim(
                    entry["qid_bytes"], entry["params"]
                )
                if sim_ok:
                    txs.append(entry["tx"])
                    hits += 1
                    continue
                # Cached calldata would revert on-chain now (almost
                # always: prior settlement that the subgraph hasn't
                # reindexed yet). Drop it and skip this question
                # this cycle -- a fresh rebuild would re-simulate
                # against the same on-chain state and fail
                # identically.
                del cache[qid]
                stale_evictions += 1
                continue
            new_entry = yield from self._try_build_single_claim(resp)
            if new_entry is not None:
                cache[qid] = new_entry
                txs.append(new_entry["tx"])

        self.context.logger.info(
            f"{SKILL_LOG_PREFIX} fetched {len(responses)} responses "
            f"({len(unique_responses)} unique questions), prepared "
            f"{len(txs)} claim txs (cache: {hits} hit, {len(evicted)} "
            f"evicted by subgraph, {stale_evictions} evicted as stale)"
        )
        return txs

    def _try_build_single_claim(
        self, resp: Dict[str, Any]
    ) -> Generator[None, None, Optional[Dict[str, Any]]]:
        """Build a claimWinnings cache entry for one response.

        Returns a cache-entry dict with shape
        ``{"qid_bytes": bytes, "params": Tuple[...], "tx": Dict[str, Any]}``
        on success, ``None`` on skip. The ``"tx"`` field is the
        multisend operation dict the caller appends to the batch; the
        ``"qid_bytes"`` and ``"params"`` fields are retained so the
        ``_build_claim_txs`` resume cache can re-run ``_simulate_claim``
        on later cycles without re-running the (expensive) full build.
        """
        # Realitio subgraph composite ids are "{contract_address}-{question_id}".
        question = resp.get("question") or {}
        question_id_hex = question["id"].split("-")[-1]
        question_id_bytes = bytes.fromhex(question_id_hex.replace("0x", ""))

        # Per-question lower bound for the eth_getLogs window, minus 1
        # as a defensive margin against subgraph indexing skew.
        created_block_raw = question.get("createdBlock")
        if created_block_raw is None:
            self.context.logger.warning(
                f"{SKILL_LOG_PREFIX} missing createdBlock for "
                f"question {question_id_hex}, skipping"
            )
            return None
        from_block = max(0, int(created_block_raw) - 1)

        # Bound the upper end using the subgraph's createdTimestamp and
        # answerFinalizedTimestamp. Without this, eth_getLogs is called
        # with to_block="latest" which can span millions of blocks for
        # old questions and be rejected or time out by RPC providers.
        to_block = self._compute_to_block(question_id_hex, question, from_block)
        if to_block is None:
            return None

        claim_params = yield from self._get_claim_params(
            question_id_bytes, from_block, to_block
        )
        if claim_params is None:
            return None
        simulation_ok = yield from self._simulate_claim(question_id_bytes, claim_params)
        if not simulation_ok:
            return None
        claim_tx = yield from self._build_claim_tx(question_id_bytes, claim_params)
        if claim_tx is None:
            return None

        bond = int(resp.get("bond", 0))
        self.context.logger.info(
            f"{SKILL_LOG_PREFIX} claiming question {question_id_hex} "
            f"(bond: {wei_to_str(bond)})"
        )
        return {
            "qid_bytes": question_id_bytes,
            "params": claim_params,
            "tx": claim_tx,
        }

    def _get_claimable_responses(
        self,
    ) -> Generator[None, None, List[Dict[str, Any]]]:
        """Query the Realitio subgraph for finalized, still-unclaimed responses.

        :yield: subgraph HTTP requests during the build.
        :return: list of response dicts (possibly empty), bounded by batch_size.
        """
        safe = self.synchronized_data.safe_contract_address.lower()
        now = self.last_synced_timestamp
        response = yield from self.get_realitio_subgraph_result(
            query=CLAIMABLE_RESPONSES_QUERY.substitute(
                safe=safe,
                batch_size=self.params.realitio_withdraw_bonds_batch_size,
                current_timestamp=now,
            )
        )
        if response is None:
            self.context.logger.warning(
                f"{SKILL_LOG_PREFIX} Realitio subgraph query failed"
            )
            return []
        return cast(List[Dict[str, Any]], response.get("data", {}).get("responses", []))

    def _compute_to_block(
        self,
        question_id_hex: str,
        question: Dict[str, Any],
        from_block: int,
    ) -> Optional[int]:
        """Compute a bounded upper block for the LogNewAnswer scan.

        Uses the subgraph's ``Question.updatedBlock`` — the block number
        of the last state change to the question, which is the block of
        the final ``LogNewAnswer`` event. A small safety margin protects
        against subgraph indexing skew.

        :param question_id_hex: the question id (for logging).
        :param question: the subgraph question dict.
        :param from_block: the already-computed from_block.
        :return: the upper bound block, or None if the required field is
            missing.
        """
        updated_block_raw = question.get("updatedBlock")
        if updated_block_raw is None:
            self.context.logger.warning(
                f"{SKILL_LOG_PREFIX} missing updatedBlock for "
                f"{question_id_hex}, skipping"
            )
            return None
        to_block = int(updated_block_raw) + _TO_BLOCK_SAFETY_MARGIN
        # Defensive: updatedBlock must be >= createdBlock.
        return max(to_block, from_block)

    def _get_claim_params(
        self, question_id: bytes, from_block: int, to_block: int
    ) -> Generator[None, None, Optional[Any]]:
        """Get the reverse-chronological 4-tuple expected by ``Realitio.claimWinnings``.

        :param question_id: the Realitio question id (32-byte).
        :param from_block: lower bound block for the LogNewAnswer scan.
        :param to_block: upper bound block for the LogNewAnswer scan.
        :yield: contract-api requests during the build.
        :return: the 4-tuple, or ``None`` on any failure.
        """
        response = yield from self.get_contract_api_response(
            performative=ContractApiMessage.Performative.GET_STATE,  # type: ignore
            contract_address=self.params.realitio_contract,
            contract_id=str(RealitioContract.contract_id),
            contract_callable=get_callable_name(RealitioContract.get_claim_params),
            from_block=from_block,
            to_block=to_block,
            question_id=question_id,
            chunk_size=self.params.event_filtering_batch_size,
            chain_id=self.params.default_chain_id,
        )
        if response.performative != ContractApiMessage.Performative.STATE:
            self.context.logger.warning(
                f"{SKILL_LOG_PREFIX} RealitioContract.get_claim_params failed "
                f"for 0x{question_id.hex()}: performative={response.performative}"
            )
            return None

        body = response.state.body
        if "error" in body:
            self.context.logger.warning(
                f"{SKILL_LOG_PREFIX} RealitioContract.get_claim_params failed "
                f"for 0x{question_id.hex()}: {body['error']}"
            )
            return None

        answered = body.get("answered")
        if answered is None or len(answered) == 0:
            self.context.logger.warning(
                f"{SKILL_LOG_PREFIX} RealitioContract.get_claim_params failed "
                f"for 0x{question_id.hex()}: no LogNewAnswer events in window "
                f"[{from_block}, {to_block}]"
            )
            return None

        # The contract wrapper returns the raw chronological list of
        # LogNewAnswer events. claimWinnings expects a 4-tuple of
        # parallel arrays in reverse-chronological order. Without this
        # transform the calldata is malformed and the simulation always
        # reverts.
        return assemble_claim_params(cast(List[Dict[str, Any]], answered))

    def _simulate_claim(
        self,
        question_id: bytes,
        claim_params: Any,
    ) -> Generator[None, None, bool]:
        """Simulate the claimWinnings call to check it would succeed."""
        safe_address = self.synchronized_data.safe_contract_address
        response = yield from self.get_contract_api_response(
            performative=ContractApiMessage.Performative.GET_STATE,  # type: ignore
            contract_address=self.params.realitio_contract,
            contract_id=str(RealitioContract.contract_id),
            contract_callable=get_callable_name(
                RealitioContract.simulate_claim_winnings
            ),
            question_id=question_id,
            claim_params=claim_params,
            sender_address=safe_address,
            chain_id=self.params.default_chain_id,
        )
        if response.performative != ContractApiMessage.Performative.STATE:
            self.context.logger.warning(
                f"{SKILL_LOG_PREFIX} RealitioContract.simulate_claim_winnings failed "
                f"for 0x{question_id.hex()}"
            )
            return False

        return bool(response.state.body.get("data", False))

    def _build_claim_tx(
        self,
        question_id: bytes,
        claim_params: Any,
    ) -> Generator[None, None, Optional[Dict[str, Any]]]:
        """Build the encoded Realitio.claimWinnings() call."""
        response = yield from self.get_contract_api_response(
            performative=ContractApiMessage.Performative.GET_STATE,  # type: ignore
            contract_address=self.params.realitio_contract,
            contract_id=str(RealitioContract.contract_id),
            contract_callable=get_callable_name(RealitioContract.build_claim_winnings),
            question_id=question_id,
            claim_params=claim_params,
            chain_id=self.params.default_chain_id,
        )
        if response.performative != ContractApiMessage.Performative.STATE:
            self.context.logger.warning(
                f"{SKILL_LOG_PREFIX} RealitioContract.build_claim_winnings failed "
                f"for 0x{question_id.hex()}"
            )
            return None

        data = response.state.body["data"]
        return {
            "to": self.params.realitio_contract,
            "data": data,
            "value": ETHER_VALUE,
        }

    def _maybe_build_withdraw_tx(
        self,
    ) -> Generator[None, None, Optional[Dict[str, Any]]]:
        """Build a Realitio withdraw tx if internal balance exceeds threshold."""
        safe_address = self.synchronized_data.safe_contract_address
        response = yield from self.get_contract_api_response(
            performative=ContractApiMessage.Performative.GET_STATE,  # type: ignore
            contract_address=self.params.realitio_contract,
            contract_id=str(RealitioContract.contract_id),
            contract_callable="balance_of",
            address=safe_address,
            chain_id=self.params.default_chain_id,
        )
        if response.performative != ContractApiMessage.Performative.STATE:
            self.context.logger.warning(
                f"{SKILL_LOG_PREFIX} RealitioContract.balance_of failed"
            )
            return None

        balance = cast(int, response.state.body["data"])

        if balance < self.params.min_realitio_withdraw_balance:
            return None

        # Build withdraw tx.
        response = yield from self.get_contract_api_response(
            performative=ContractApiMessage.Performative.GET_STATE,  # type: ignore
            contract_address=self.params.realitio_contract,
            contract_id=str(RealitioContract.contract_id),
            contract_callable="build_withdraw_tx",
            chain_id=self.params.default_chain_id,
        )
        if response.performative != ContractApiMessage.Performative.STATE:
            self.context.logger.warning(
                f"{SKILL_LOG_PREFIX} RealitioContract.build_withdraw_tx failed"
            )
            return None

        data = response.state.body["data"]
        self.context.logger.info(
            f"{SKILL_LOG_PREFIX} Realitio balance {wei_to_str(balance)}, "
            f"appending withdraw"
        )
        return {
            "to": self.params.realitio_contract,
            "data": data,
            "value": ETHER_VALUE,
        }
