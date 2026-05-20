# omen-protocol

Shared Omen prediction market contracts and recovery skills for [Open Autonomy](https://github.com/valory-xyz/open-autonomy) agents. Consolidates the packages used by [valory-xyz/trader](https://github.com/valory-xyz/trader), [valory-xyz/market-creator](https://github.com/valory-xyz/market-creator), and [valory-xyz/market-resolver](https://github.com/valory-xyz/market-resolver) so they are sourced from one canonical upstream.

## What's in this repo

### Contracts

| Package | Public ID | Description |
|---|---|---|
| Contract | `valory/realitio` | Realitio oracle wrapper (question creation, answer submission, challenge periods, bond withdrawal). |
| Contract | `valory/realitio_proxy` | Proxy that triggers market resolution by reporting Realitio's answer to the conditional tokens layer. |
| Contract | `valory/conditional_tokens` | Gnosis ConditionalTokens (ERC1155) for binary outcome positions. |
| Contract | `valory/fpmm` | Gnosis FixedProductMarketMaker for liquidity provisioning and trading. |

### Skills

| Package | Public ID | Description |
|---|---|---|
| Skill | `valory/omen_realitio_withdraw_bonds_abci` | Withdraws Realitio answerer bonds via `claimWinnings` and `withdraw`. |
| Skill | `valory/omen_ct_redeem_tokens_abci` | Redeems winning ConditionalTokens positions on resolved markets, optionally calling `RealitioProxy.resolve` first. |
| Skill | `valory/omen_fpmm_remove_liquidity_abci` | Removes LP shares from Omen FPMM markets at close, handling the four market states with optional `mergePositions` calls. |

All packages live under `packages/` and are published to IPFS via the Open Autonomy registry.

## Requirements

- Python `>=3.10, <3.15`
- [uv](https://docs.astral.sh/uv/)

## Install

```bash
uv sync
source .venv/bin/activate
autonomy packages sync
```

## Development

```bash
make format          # black + isort
make code-checks     # lint + type check
make security        # bandit + safety + gitleaks
make generators      # regenerate hashes
make common-checks-1 # hash + copyright + docs
make common-checks-2 # ABCI specs + dependencies + handlers
```

See `CONTRIBUTING.md` for the full pre-PR checklist.

## License

Licensed under [Apache License 2.0](LICENSE).
