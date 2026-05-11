# Cloud Deployment

The default hosted shape for this project is a budget AWS demo, not a managed
microservice stack. With only a handful of organic users, the goal is to keep a
credible, interviewer-recognizable deployment under about **$50/month** by
running the app as one small AWS-hosted appliance.

Managed services such as RDS, ElastiCache, OpenSearch Service, ClickHouse Cloud, and
an Application Load Balancer are useful later, but they do not fit the $50/month
target together.

## Budget AWS Shape

| Need | Budget default | Why |
| --- | --- | --- |
| Host | One EC2 Graviton instance, ideally `t4g.medium` | Recognizable AWS, enough room for API, workers, Postgres, and Redis. |
| Runtime | Docker Compose on the EC2 host | Simple to understand, deploy, restart, and demo. |
| API + dashboard | Repo Docker image | The `Dockerfile` builds the Vite frontend into the FastAPI image. |
| Database | Postgres container on a gp3 EBS volume | RDS is easier, but compute, storage, and backups push the bill past budget. |
| Cache | Local Redis container, capped at 128 MB | Good enough for dashboard cache without ElastiCache. |
| Search | Postgres fallback search | OpenSearch is an upgrade path, not a budget default. |
| Raw analytics | Postgres projections with short retention | ClickHouse is valuable later, but raw tape must be aggressively compacted first. |
| TLS | Caddy or nginx on the same EC2 host | Avoid the monthly ALB floor. |
| Backups | Nightly compressed `pg_dump` to S3 | Cheap and easy to inspect/restore. |
| Secrets | `.env` on the host or SSM Parameter Store standard parameters | Avoid Secrets Manager per-secret charges for the demo. |
| Logs | Local log rotation, optional minimal CloudWatch | Keep noisy worker logs from becoming a surprise bill. |

### Redis (budget)

`docker-compose.budget.yml` runs Redis with `--maxmemory 128mb` and
`--maxmemory-policy allkeys-lru` (no AOF/RDB persistence in that profile). Any
key, including `dashboard:*` cache entries and rate-limit counters, can be evicted
when usage approaches the cap. Symptoms: first dashboard paint is slow even
though the cache warmer runs, or `redis-cli --scan --pattern 'dashboard:*'`
returns nothing until traffic repopulates keys. Mitigations: raise `maxmemory`,
run a second Redis for hot cache only, or accept occasional cold reads from
Postgres.

Dashboard `dashboard:*` keys: user requests that hit Redis return cached bytes
without extending TTL; the pipeline’s `warm_dashboard_cache_once` path forces
rebuild + `SETEX` each warm so tiles and pipeline health stay current between
TTL expirations.

`docker-compose.budget.yml` is the budget profile. It runs:

- `postgres`
- `redis`
- `app`
- `pipeline`

The `pipeline` service runs poller, WebSocket ingest, news ingest, scoring
materializers, retention maintenance, anomaly retention, chart-history
compaction, and dashboard cache warming under the lightweight supervisor. It
keeps L2/order-book subscriptions narrow, keeps raw ticker snapshots gated by
storage tier, and compacts old closed-market chart buckets after a 7-day grace.

## Budget Environment

Start from `.env.budget.example`:

```bash
cp .env.budget.example .env
```

Then set:

```text
POSTGRES_PASSWORD=<strong password>
KALSHI_API_KEY_ID=<kalshi key id>
KALSHI_PRIVATE_KEY_PEM=<kalshi private key pem text>
```

Important budget defaults:

```text
KALSHI_RAW_BACKEND=postgres
KALSHI_BOOK_MARKET_LIMIT=5
KALSHI_WS_WORKER_COUNT=1
KALSHI_CHART_HISTORY_ENABLED=1
KALSHI_WS_TICKER_SNAPSHOTS_ENABLED=1
RETENTION_BOOK_EVENTS_MAX_AGE_DAYS=1
RETENTION_SNAPSHOT_OBSERVE_MAX_AGE_DAYS=1
RETENTION_SNAPSHOT_SAMPLED_MAX_AGE_DAYS=3
RETENTION_SNAPSHOT_HOT_MAX_AGE_DAYS=14
```

These settings intentionally reduce ingestion and storage. Historical review
should rely on retained projections, trade flags, news links, anomaly records,
and promoted evidence windows rather than every raw book update forever.

## First Budget Deploy

1. Launch an Ubuntu EC2 instance in one public subnet.
2. Use `t4g.medium` if the ARM build works for your stack. Use a small x86
   instance only if a dependency forces it.
3. Attach a 100-150 GB gp3 root/EBS volume. Use 100 GB for lowest cost, 150 GB
   for more breathing room.
4. Security group: allow `22` from your IP, `80`/`443` public, and keep Postgres
   closed to the internet.
5. Install Docker and the Docker Compose plugin.
6. Clone the repo and create `.env` from `.env.budget.example`.
7. Start only the data containers first:

```bash
docker compose -f docker-compose.budget.yml up -d postgres redis
```

8. Build the app image and run migrations:

```bash
docker compose -f docker-compose.budget.yml build app pipeline
docker compose -f docker-compose.budget.yml exec app python -m alembic upgrade head
```

9. If you are restoring your local data, restore a compressed dump into the
   `postgres` container before starting the `pipeline` service.
10. Start the app and pipeline:

```bash
docker compose -f docker-compose.budget.yml up -d app pipeline
```

11. Put Caddy or nginx on the host for HTTPS and proxy it to `127.0.0.1:8000`.
12. Set an AWS Budget alert at `$40` and `$50`.
13. Check:

```text
/api/dashboard/pipeline-health
/api/dashboard/storage-health
```

## Public Edge Defaults

The budget compose file binds the app to `127.0.0.1:8000` on the host. Public
traffic should enter through Caddy on ports `80`/`443`; keep port `8000` closed
in the EC2 security group.

Copy `deploy/Caddyfile.example` to `/etc/caddy/Caddyfile`, replace
`yourdomain.com`, and reload Caddy:

```bash
sudo cp deploy/Caddyfile.example /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

The example enables gzip/zstd compression, proxies to the local app, and writes
rotated access logs to:

```text
/var/log/caddy/predictionmarkets-access.log
```

API rate limiting is enabled by default and uses Redis when available. Tune it
from `.env`:

```text
RATE_LIMIT_ENABLED=true
RATE_LIMIT_DEFAULT_PER_MINUTE=120
RATE_LIMIT_EXPENSIVE_PER_MINUTE=30
RATE_LIMIT_HEALTH_PER_MINUTE=600
```

Static frontend assets are not app-rate-limited. The lower "expensive" bucket
applies to search and news endpoints, which can fan out into heavier database or
provider work.

The budget pipeline disables GDELT by default (`--no-gdelt`) and leans on
RSS/official APIs. GDELT's public endpoint can rate-limit broad unattended
sweeps from cloud hosts. Set a descriptive `NEWS_USER_AGENT` in `.env` so
official feeds such as SEC/BLS can identify the project and contact owner.

## Storage Guardrails

The current local data shape can grow faster than the budget host can store if
raw tables are allowed to run without pruning. Keep these rules strict:

- Keep raw order-book/L2 storage tiny. A `KALSHI_BOOK_MARKET_LIMIT` of `0-5` is
  realistic for a $50 deployment.
- Keep book events around for about one day unless a case is promoted.
- Keep observe/sample snapshot history short; store durable conclusions, not
  every tick.
- Run retention maintenance frequently with bounded batches. Snapshot pruning
  should require matching `market_price_history` coverage and must preserve
  latest-snapshot/anomaly references.
- Run chart-history compaction for closed/resolved markets after the 7-day
  grace so price charts can use compact buckets instead of raw snapshots.
- Send compressed Postgres dumps to S3 and expire old backups after 7-14 days.
- Prefer retained evidence, market metrics, news links, trade flags, and anomaly
  summaries for demos.

If disk usage crosses 80%, first tighten retention and prune raw tables. Large
deletes may not immediately return disk to the operating system; schedule a
maintenance window for dump/restore or `VACUUM FULL` if the physical volume
needs to shrink.

## Cost Planning

These are rough US-East planning numbers as of April 2026. Re-run the AWS
calculator before committing spend.

| Piece | Budget estimate | Notes |
| --- | ---: | --- |
| EC2 `t4g.medium` | about $25/month | 2 vCPU, 4 GB RAM; enough for the demo if retention stays tight. |
| gp3 EBS, 100 GB | about $8/month | Lowest practical starting point. |
| gp3 EBS, 150 GB | about $12/month | Safer with the current local database size and growth risk. |
| Public IPv4 address | about $3.65/month | AWS charges for public IPv4 whether attached or idle. |
| S3 backups | about $1-$3/month | Depends on dump size and backup retention. |
| Route 53 hosted zone | about $0.50/month | Domain registration is separate annual spend. |
| CloudWatch alarms/logs | $0-$5/month | Keep log retention short; local logs are fine for the demo. |

Practical total:

- **Lean 100 GB plan:** about **$38-$44/month**
- **Safer 150 GB plan:** about **$42-$48/month**

Do not add RDS, ElastiCache, OpenSearch Service, ClickHouse Cloud, ALB, NAT Gateway,
or Secrets Manager to the budget profile unless you are deliberately moving out
of the $50/month cap.

## Container Image

The root `Dockerfile` builds the Vite frontend and serves it from the FastAPI
container:

```bash
docker build -t predictionmarkets:local .
docker run --rm -p 8000:8000 --env-file .env predictionmarkets:local
```

For the budget EC2 deployment, Docker Compose can build this image directly on
the host. If you want a more AWS-native artifact later, push the same image to
Amazon ECR:

```bash
aws ecr create-repository --repository-name predictionmarkets
aws ecr get-login-password --region <region> \
  | docker login --username AWS --password-stdin <account-id>.dkr.ecr.<region>.amazonaws.com
docker tag predictionmarkets:local <account-id>.dkr.ecr.<region>.amazonaws.com/predictionmarkets:<tag>
docker push <account-id>.dkr.ecr.<region>.amazonaws.com/predictionmarkets:<tag>
```

## Managed Upgrade Path

Use this when the app has real users, durable uptime requirements, or enough
data volume to justify managed services:

| Need | Managed service | Why |
| --- | --- | --- |
| API + React dashboard | Amazon ECS on AWS Fargate | Runs the repo image without managing EC2 hosts. |
| Postgres control plane | Amazon RDS for PostgreSQL | Managed backups, snapshots, Multi-AZ/read-replica path. |
| Dashboard cache | Amazon ElastiCache for Redis OSS or Valkey | Managed Redis-compatible cache for dashboard payloads. |
| Search | Amazon OpenSearch Service | AWS-managed OpenSearch with autocomplete and hybrid/vector-search runway. |
| Raw analytical tape | ClickHouse Cloud on AWS | Keeps high-volume raw/event analytics out of Postgres. |
| Secrets | AWS Secrets Manager | Managed Kalshi key, DB credentials, and service credentials. |
| Logs | Amazon CloudWatch Logs | Fargate-native container logs. |

Use one image with different commands:

| ECS service/task | Command |
| --- | --- |
| `api` service | default Docker command: `python -m uvicorn app.main:app --host 0.0.0.0 --port 8000` |
| `market-poller` service | `python -m scripts.poll_markets --interval 300 --batch 500` |
| `ws-trade-feed` service | `python -m scripts.run_ws_ticker_consumer` |
| `news-pipeline` service | `python -m scripts.run_news_surveillance_pipeline --watch --interval-seconds 300` |
| scheduled `retention-maintenance` task | `python -m scripts.run_retention_maintenance --execute --analyze` |
| scheduled `chart-history-compaction` task | `python -m scripts.run_chart_history_compaction --execute --replace-source-rows` |
| scheduled `clickhouse-retention-check` task | `python -m scripts.verify_clickhouse_retention --strict` |
| one-shot migration task | `python -m alembic upgrade head` |
| one-shot projection backfill | `python -m scripts.backfill_market_metrics` |

The managed setup is a later upgrade path, not the current budget target.

## Tradeoffs

- The budget plan is intentionally less managed. You save money by accepting
  more responsibility for backups, OS patching, and disk monitoring.
- Postgres remains the source of truth for markets, projections, flags, and
  cases. Raw analytical storage is compacted aggressively.
- Search stays rebuildable and Postgres-backed for now. OpenSearch can come
  back when discovery quality matters more than the bill.
- ClickHouse is still the right architecture for high-volume raw analytics, but
  it should wait until retention is proven and monthly spend can exceed $50.
- Separate ECS services are cleaner operationally. A single EC2 Compose host is
  the better fit for a recruiter/demo budget.

Useful pricing pages:

- Amazon EC2 On-Demand: https://aws.amazon.com/ec2/pricing/on-demand/
- Amazon EBS: https://aws.amazon.com/ebs/pricing/
- Public IPv4 address pricing: https://aws.amazon.com/vpc/pricing/
- Amazon S3: https://aws.amazon.com/s3/pricing/
- Amazon Route 53: https://aws.amazon.com/route53/pricing/
- Amazon CloudWatch: https://aws.amazon.com/cloudwatch/pricing/
