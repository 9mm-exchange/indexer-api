# PulseChain Token Indexer

A high-performance API for indexing ERC20 token Transfer events on PulseChain and querying EOA holder balances.

## Features

- **EOA-Only Filtering**: Automatically filters out contracts, LPs, and zero address
- **Continuous Sync**: Indexes from start block and keeps syncing new blocks forever
- **Resumable**: Tracks progress in SQLite, resumes from last indexed block on restart
- **Pre-computed Balances**: Instant `/holders` response with pre-computed balance table
- **Response Caching**: 30-second TTL cache for frequently accessed endpoints
- **Gzip Compression**: Automatic compression for large JSON responses
- **Batch RPC**: Batch JSON-RPC calls for efficient EOA checking (100 addresses/batch)
- **Retry with Backoff**: Exponential backoff retry for RPC failures
- **Prometheus Metrics**: Built-in `/metrics` endpoint for monitoring
- **WAL Mode**: SQLite Write-Ahead Logging for better concurrent performance
- **Kubernetes Ready**: Includes Dockerfile and K8s manifests for production deployment

## Quick Start

### Local Development

1. **Clone and setup environment:**

```bash
cd Indexer
cp .env.example .env
```

2. **Install dependencies:**

```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

3. **Run the server:**

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
docker run -p 8000:8000 -v indexer_data:/data token-indexer
```

## API Endpoints

### GET /holders

Returns all EOA token holders with their balances. Response is cached for 30 seconds and gzip compressed.

**Response:**
```json
{
  "token_address": "0x7b39712Ef45F7dcED2bBDF11F3D5046bA61dA719",
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

Health check endpoint for Kubernetes probes.

**Response:**
```json
{
  "status": "healthy",
  "last_indexed_block": 24903456,
  "sync_in_progress": false
}
```

### GET /status

Detailed sync status.

**Response:**
```json
{
  "last_indexed_block": 24900000,
  "chain_head_block": 24903456,
  "blocks_behind": 3456,
  "is_syncing": true,
  "addresses_checked": 50000
}
```

### GET /stats

Indexer statistics.

**Response:**
```json
{
  "token_address": "0x7b39712Ef45F7dcED2bBDF11F3D5046bA61dA719",
  "total_transfers_indexed": 150000,
  "eoa_holder_count": 43000,
  "total_addresses_checked": 55000,
  "total_eoa_addresses": 43000,
  "total_contract_addresses": 12000,
  "last_indexed_block": 24903456,
  "sync_in_progress": false,
  "start_block": 20326117
}
```

### GET /metrics

Prometheus metrics endpoint for monitoring.

**Metrics exposed:**
- `indexer_requests_total` - Total HTTP requests by method, endpoint, status
- `indexer_request_latency_seconds` - Request latency histogram
- `indexer_holder_count` - Current number of EOA holders
- `indexer_transfer_count` - Total indexed transfers
- `indexer_last_indexed_block` - Last indexed block number
- `indexer_blocks_behind` - Blocks behind chain head
- `indexer_sync_in_progress` - Sync status (1=syncing, 0=idle)

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

3. **Update the Ingress (optional):**

Edit `k8s/ingress.yaml` and replace `indexer.yourdomain.com` with your domain.

4. **Apply the manifests:**

```bash
kubectl apply -f k8s/
```

5. **Check status:**

```bash
kubectl get pods -l app=token-indexer
kubectl logs -f deployment/token-indexer
```

### Prometheus Integration

The deployment includes Prometheus scrape annotations. If you have Prometheus Operator installed, metrics will be automatically scraped from `/metrics`.

### Configuration

Configuration is managed via the ConfigMap in `k8s/configmap.yaml`:

| Variable | Description | Default |
|----------|-------------|---------|
| `RPC_URL` | PulseChain RPC endpoint | `https://rpc.pulsechain.com` |
| `TOKEN_ADDRESS` | Token contract address | `0x7b39712Ef45F7dcED2bBDF11F3D5046bA61dA719` |
| `START_BLOCK` | Block to start indexing from | `20326117` |
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
│  /holders │ /health │ /status │ /stats │ /metrics         │
│     ↓          ↓         ↓         ↓                       │
│  [Cache]   [Direct]  [Direct]  [Cache]                     │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    Background Indexer                        │
│  - Fetches Transfer events in batches (10k blocks)          │
│  - Batch EOA checking (100 addresses/request)               │
│  - Retry with exponential backoff                           │
│  - Updates pre-computed balances incrementally              │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                 SQLite Database (WAL Mode)                   │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐        │
│  │  transfers   │ │   balances   │ │address_types │        │
│  │  (indexed)   │ │(pre-computed)│ │  (EOA cache) │        │
│  └──────────────┘ └──────────────┘ └──────────────┘        │
│  ┌──────────────┐                                           │
│  │  sync_state  │                                           │
│  └──────────────┘                                           │
└─────────────────────────────────────────────────────────────┘
```

## Performance Optimizations

| Feature | Benefit |
|---------|---------|
| Pre-computed balances | O(n) read vs O(n²) calculation on each request |
| Response caching | 30s TTL eliminates redundant DB queries |
| Gzip compression | ~90% smaller response for 43k holders |
| Batch RPC calls | 100x fewer HTTP requests for EOA checking |
| SQLite WAL mode | Better concurrent read/write performance |
| Incremental updates | Only process new transfers, not entire history |

## Notes

- Initial sync may take several hours depending on RPC performance
- The indexer is resumable - continues from where it left off after restart
- ~43,000 holders expected for this token
- SQLite database is persisted via PVC in Kubernetes

## License

MIT
