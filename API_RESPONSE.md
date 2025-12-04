# 9MM Token Holders API Response

## Endpoint

```
GET https://index-api.9mm.pro/holders?chain_id={chain_id}
```

## Query Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `chain_id` | Yes | Chain ID (1=Ethereum, 8453=Base, 146=Sonic, 369=PulseChain) |
| `include_contracts` | No | Include contract addresses (default: false) |

## Response

```json
{
  "chain_id": 1,
  "chain_name": "Ethereum",
  "token_address": "0x824b556259C69d7F2F12F3b21811Cfb00CE126aF",
  "holder_count": 1748,
  "last_indexed_block": 23936539,
  "sync_in_progress": true,
  "holders": [
    {
      "address": "0xe784643F9C2eC47F83D87E5823Fe5b19FFe40FE7",
      "balance": "158401086283485273788737"
    },
    {
      "address": "0x3193db2D06Ef42Ed7f517dAAC491bac9Ecd0E7c8",
      "balance": "8082621416368520943156"
    }
  ]
}
```

## Response Fields

| Field | Type | Description |
|-------|------|-------------|
| `chain_id` | integer | Chain identifier |
| `chain_name` | string | Human-readable chain name |
| `token_address` | string | 9MM token contract address |
| `holder_count` | integer | Total number of EOA holders |
| `last_indexed_block` | integer | Most recent indexed block |
| `sync_in_progress` | boolean | Whether indexer is actively syncing |
| `holders` | array | List of holder objects |
| `holders[].address` | string | Wallet address (checksummed) |
| `holders[].balance` | string | Token balance in wei (18 decimals) |

## Chain IDs

| Chain | ID |
|-------|-----|
| Ethereum | 1 |
| Base | 8453 |
| Sonic | 146 |
| PulseChain | 369 |

## Example Requests

```bash
# Get Ethereum holders
curl https://index-api.9mm.pro/holders?chain_id=1

# Get Base holders including contracts
curl https://index-api.9mm.pro/holders?chain_id=8453&include_contracts=true
```

## Notes

- Balance is returned in **wei** (divide by 10^18 for token amount)
- Holders are sorted by balance in descending order
- Only addresses with balance > 0 are included
- By default, only EOA (Externally Owned Accounts) are returned

