# Multi-Chain Token Indexer

A high-performance API for indexing ERC20 token Transfer events across multiple EVM chains and querying EOA holder balances.

## Features

- **Multi-Chain Support**: Index 9MM tokens across multiple EVM chains simultaneously
- **EOA-Only Filtering**: Automatically filters out contracts, LPs, and zero address
- **Continuous Sync**: Indexes from start block and keeps syncing new blocks forever for each chain
- **Resumable**: Tracks progress per chain in SQLite, resumes from last indexed block on restart
- **Pre-computed Balances**: Instant `/holders` response with pre-computed balance table per chain
- **Response Caching**: 30-second TTL cache for frequently accessed endpoints
- **Gzip Compression**: Automatic compression for large JSON responses
- **Batch RPC**: Batch JSON-RPC calls for efficient EOA checking (100 addresses/batch)
- **Retry with Backoff**: Exponential backoff retry for RPC failures
- **Prometheus Metrics**: Built-in `/metrics` endpoint for monitoring (per-chain metrics)
- **WAL Mode**: SQLite Write-Ahead Logging for better concurrent performance
- **Kubernetes Ready**: Includes Dockerfile and K8s manifests for production deployment

## Supported Chains

The indexer supports any EVM-compatible chain. Currently indexing 9MM token on:
- **PulseChain** (369) - `0x7b39712Ef45F7dcED2bBDF11F3D5046bA61dA719`
- **Base** (8453) - `0xe290816384416fb1dB9225e176b716346dB9f9fE`
- **Sonic** (146) - `0xC5cB0B67D24d72b9D86059344c88Fb3cE93BF37C`
- **Ethereum** (1) - `0x824b556259C69d7F2F12F3b21811Cfb00CE126aF`

## API Quick Reference

| Endpoint | Description |
|----------|-------------|
| `GET /chains` | List all configured chains |
| `GET /holders?chain_id=369` | Get all token holders for a chain |
| `GET /holders?chain_id=369&include_contracts=true` | Include contract addresses |
| `GET /status` | Sync status for all chains |
| `GET /status?chain_id=369` | Sync status for specific chain |
| `GET /stats` | Statistics for all chains |
| `GET /stats?chain_id=369` | Statistics for specific chain |
| `GET /health` | Health check (for K8s probes) |
| `GET /metrics` | Prometheus metrics |

**Live API:** `https://index-api.9mm.pro`

**Examples:**
```bash
# Get PulseChain holders
curl https://index-api.9mm.pro/holders?chain_id=369

# Check sync progress
curl https://index-api.9mm.pro/status

# Get all chains
curl https://index-api.9mm.pro/chains
```

## Quick Start

### Local Development

1. **Clone and setup environment:**

```bash
cd Indexer
cp .env.example .env
```

2. **Configure chains in `.env`:**

Edit `.env` and configure your chains using the `CHAINS_CONFIG` JSON format:

```bash
CHAINS_CONFIG=[
  {"chain_id": 1, "chain_name": "Ethereum", "rpc_url": "https://eth.llamarpc.com", "token_address": "0x...", "start_block": 0},
  {"chain_id": 137, "chain_name": "Polygon", "rpc_url": "https://polygon-rpc.com", "token_address": "0x...", "start_block": 0}
]
```

3. **Install dependencies:**

```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

4. **Run the server:**

```bash
uvicorn app.main:app --reload
```

The API will be available at `http://localhost:8000`

### Docker

```bash
# Build and run with docker-compose
docker-compose up -d

# Or build manually
docker build -t token-indexer .
docker run -p 8000:8000 -v indexer_data:/data \
  -e CHAINS_CONFIG='[{"chain_id": 1, "chain_name": "Ethereum", "rpc_url": "https://eth.llamarpc.com", "token_address": "0x...", "start_block": 0}]' \
  token-indexer
```

## Configuration

### RPC Providers

For reliable indexing, we recommend using **Alchemy** (free tier available):
- Sign up at https://alchemy.com
- Create apps for Ethereum, Base, and Sonic
- Use your API key in the RPC URLs

**Why Alchemy?** Public RPCs often truncate `eth_getLogs` results, causing missed transfers.

### Multi-Chain Configuration

The indexer supports multiple configuration methods:

#### Method 1: JSON String (Recommended)

Set `CHAINS_CONFIG` as a JSON array:

```bash
CHAINS_CONFIG=[{"chain_id": 1, "chain_name": "Ethereum", "rpc_url": "https://eth.llamarpc.com", "token_address": "0x...", "start_block": 0}, {"chain_id": 137, "chain_name": "Polygon", "rpc_url": "https://polygon-rpc.com", "token_address": "0x...", "start_block": 0}]
```

#### Method 2: Individual Environment Variables

```bash
CHAIN_IDS=1,137,56
CHAIN_1_NAME=Ethereum
CHAIN_1_RPC_URL=https://eth.llamarpc.com
CHAIN_1_TOKEN_ADDRESS=0x...
CHAIN_1_START_BLOCK=0
CHAIN_137_NAME=Polygon
CHAIN_137_RPC_URL=https://polygon-rpc.com
CHAIN_137_TOKEN_ADDRESS=0x...
CHAIN_137_START_BLOCK=0
```

#### Method 3: Legacy Single Chain (Backward Compatible)

```bash
RPC_URL=https://rpc.pulsechain.com
TOKEN_ADDRESS=0x7b39712Ef45F7dcED2bBDF11F3D5046bA61dA719
START_BLOCK=20326117
CHAIN_ID=369
CHAIN_NAME=PulseChain
```

## API Endpoints

### GET /chains

Get all registered chains.

**Response:**
```json
{
  "chains": [
    {
      "chain_id": 1,
      "chain_name": "Ethereum",
      "token_address": "0x...",
      "start_block": 0,
      "is_active": true
    }
  ]
}
```

### GET /holders?chain_id={chain_id}

Returns all EOA token holders with their balances for a specific chain. Response is cached for 30 seconds and gzip compressed.

**Query Parameters:**
- `chain_id` (required): Chain ID to query
- `include_contracts` (optional): Include contract addresses (default: false)

**Response:**
```json
{
  "chain_id": 1,
  "chain_name": "Ethereum",
  "token_address": "0x...",
  "holder_count": 43000,
  "last_indexed_block": 24903456,
  "sync_in_progress": false,
  "holders": [
    {"address": "0x...", "balance": "1000000000000000000"},
    ...
  ]
}
```

### GET /health

Health check endpoint for Kubernetes probes. Returns status of all chains.

**Response:**
```json
{
  "status": "healthy",
  "chains": [
    {
      "chain_id": 1,
      "chain_name": "Ethereum",
      "token_address": "0x...",
      "start_block": 0,
      "is_active": true
    }
  ],
  "any_syncing": false
}
```

### GET /status?chain_id={chain_id}

Detailed sync status. If `chain_id` is not provided, returns status for all chains.

**Query Parameters:**
- `chain_id` (optional): Filter by chain ID

**Response (single chain):**
```json
{
  "chains": [
    {
      "chain_id": 1,
      "chain_name": "Ethereum",
  "last_indexed_block": 24900000,
  "chain_head_block": 24903456,
  "blocks_behind": 3456,
  "is_syncing": true,
  "addresses_checked": 50000
    }
  ]
}
```

### GET /stats?chain_id={chain_id}

Indexer statistics. If `chain_id` is not provided, returns stats for all chains.

**Query Parameters:**
- `chain_id` (optional): Filter by chain ID

**Response (single chain):**
```json
{
  "chain_id": 1,
  "chain_name": "Ethereum",
  "token_address": "0x...",
  "total_transfers_indexed": 150000,
  "eoa_holder_count": 43000,
  "total_addresses_checked": 55000,
  "total_eoa_addresses": 43000,
  "total_contract_addresses": 12000,
  "last_indexed_block": 24903456,
  "sync_in_progress": false,
  "start_block": 0
}
```

**Response (all chains):**
```json
{
  "chains": [
    {
      "chain_id": 1,
      "chain_name": "Ethereum",
      ...
    },
    {
      "chain_id": 137,
      "chain_name": "Polygon",
      ...
    }
  ]
}
```

### GET /metrics

Prometheus metrics endpoint for monitoring.

**Metrics exposed (per chain):**
- `indexer_requests_total` - Total HTTP requests by method, endpoint, status
- `indexer_request_latency_seconds` - Request latency histogram
- `indexer_holder_count{chain_id}` - Current number of EOA holders per chain
- `indexer_transfer_count{chain_id}` - Total indexed transfers per chain
- `indexer_last_indexed_block{chain_id}` - Last indexed block number per chain
- `indexer_blocks_behind{chain_id}` - Blocks behind chain head per chain
- `indexer_sync_in_progress{chain_id}` - Sync status per chain (1=syncing, 0=idle)

## Frontend (Balance Checker)

A static HTML/JS frontend is available in `balance-checker.html` to query the API and display user balances.

### Local Usage
Simply open `balance-checker.html` in your browser. Configure the API endpoint in the `API_BASE` constant if running locally.

### Deployment
The frontend is designed to be served via Nginx in Kubernetes. See `k8s-frontend/` for manifests.

## Deployment Helper Script

A `deploy.sh` script is provided to simplify Kubernetes operations.

```bash
chmod +x deploy.sh

# Deploy Backend API
./deploy.sh deploy-backend

# Deploy Frontend
./deploy.sh deploy-frontend

# Check Status
./deploy.sh status
```

## Kubernetes Deployment


### Prerequisites

- Kubernetes cluster
- `kubectl` configured
- Container registry access
- (Optional) Prometheus for metrics scraping

### Deploy

1. **Build and push the Docker image:**

```bash
docker build -t your-registry/token-indexer:latest .
docker push your-registry/token-indexer:latest
```

2. **Update the deployment:**

Edit `k8s/deployment.yaml` and replace `your-registry/token-indexer:latest` with your actual image.

3. **Configure chains in ConfigMap:**

Edit `k8s/configmap.yaml` and update the `CHAINS_CONFIG` JSON array with your chain configurations.

4. **Update the Ingress (optional):**

Edit `k8s/ingress.yaml` and replace `indexer.yourdomain.com` with your domain.

5. **Apply the manifests:**

```bash
kubectl apply -f k8s/
```

6. **Check status:**

```bash
kubectl get pods -l app=token-indexer
kubectl logs -f deployment/token-indexer
```

### Prometheus Integration

The deployment includes Prometheus scrape annotations. If you have Prometheus Operator installed, metrics will be automatically scraped from `/metrics`. Metrics are labeled by `chain_id` for multi-chain monitoring.

### Configuration

Configuration is managed via the ConfigMap in `k8s/configmap.yaml`:

| Variable | Description | Default |
|----------|-------------|---------|
| `CHAINS_CONFIG` | JSON array of chain configurations | See example in configmap.yaml |
| `BATCH_SIZE` | Blocks per batch | `10000` |
| `DATABASE_PATH` | SQLite database path | `/data/indexer.db` |

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      FastAPI Server                         │
│  ┌─────────────────────────────────────────────────────┐   │
│  │  Middleware: Gzip Compression | Metrics | CORS      │   │
│  └─────────────────────────────────────────────────────┘   │
├─────────────────────────────────────────────────────────────┤
│  /chains │ /holders │ /health │ /status │ /stats │ /metrics│
│     ↓         ↓          ↓         ↓         ↓              │
│  [Direct]  [Cache]   [Direct]  [Direct]  [Cache]          │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│              Multi-Chain Background Indexer                  │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐      │
│  │ Chain 1      │ │ Chain 137    │ │ Chain 56      │      │
│  │ Indexer      │ │ Indexer      │ │ Indexer      │      │
│  └──────────────┘ └──────────────┘ └──────────────┘      │
│  - Batch Transfer fetching (10k blocks)                  │
│  - Batch EOA checking (100 addresses/request)             │
│  - Retry with exponential backoff                         │
│  - Updates pre-computed balances incrementally            │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                 SQLite Database (WAL Mode)                  │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐        │
│  │  chains      │ │  transfers   │ │   balances   │        │
│  │  (config)    │ │(per chain)   │ │(per chain)   │        │
│  └──────────────┘ └──────────────┘ └──────────────┘        │
│  ┌──────────────┐ ┌──────────────┐                         │
│  │address_types │ │  sync_state   │                         │
│  │(per chain)   │ │ (per chain)   │                         │
│  └──────────────┘ └──────────────┘                         │
└─────────────────────────────────────────────────────────────┘
```

## Performance Optimizations

| Feature | Benefit |
|---------|---------|
| Pre-computed balances | O(n) read vs O(n²) calculation on each request |
| Response caching | 30s TTL eliminates redundant DB queries |
| Gzip compression | ~90% smaller response for large holder lists |
| Batch RPC calls | 100x fewer HTTP requests for EOA checking |
| SQLite WAL mode | Better concurrent read/write performance |
| Incremental updates | Only process new transfers, not entire history |
| Per-chain indexing | Independent sync progress per chain |

## Database Schema

The database uses chain_id as a key component:

- **chains**: Chain configurations (chain_id, chain_name, rpc_url, token_address, start_block)
- **transfers**: Transfer events indexed by chain_id
- **balances**: Pre-computed balances per chain_id
- **address_types**: EOA/contract cache per chain_id
- **sync_state**: Sync progress per chain_id

## Notes

- Initial sync may take several hours depending on RPC performance and chain size
- The indexer is resumable - continues from where it left off after restart
- Each chain syncs independently and concurrently
- SQLite database is persisted via PVC in Kubernetes
- All chains share the same database but data is isolated by chain_id

## License

MIT
