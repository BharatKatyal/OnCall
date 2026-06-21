# OnCall — Redis incident-memory plane

`search_incident_memory` is backed by **Redis Stack** (RediSearch vector index)
+ local **model2vec** embeddings (`redis_store.py`). If Redis is unreachable, the
tool falls back to the verbatim INC-0412 record so the demo never breaks.

Redis is used three ways (matching the DevPost):
- **Agent memory** — incidents persist as hashes under `incident:` across sessions.
- **Vector search** — cosine KNN over the incident corpus (`incidents_idx`).
- **Semantic cache** — a second vector index (`qcache_idx`) over past queries; a
  new query within `CACHE_SIM_THRESHOLD` (0.50) cosine of a prior one returns the
  cached result instantly. Console prints `CACHE HIT` / `cache miss` for the demo.

To reset the cache between demo runs:
```bash
docker exec oncall-redis redis-cli --scan --pattern 'qcache:*' | xargs -r docker exec oncall-redis redis-cli DEL
```

## Local Redis (Docker) — current setup

```bash
docker run -d --name oncall-redis -p 6380:6379 -p 8001:8001 redis/redis-stack:latest
```

> Host port is **6380** (not 6379) because another project's `redis:7-alpine`
> already holds 6379. `redis_store.py` defaults to `localhost:6380`.

- Redis: `localhost:6380`
- RedisInsight UI: http://localhost:8001
- Module check: `docker exec oncall-redis redis-cli MODULE LIST | grep search`

Container lifecycle:
```bash
docker start oncall-redis     # after a reboot
docker stop oncall-redis
docker rm -f oncall-redis      # remove
```

## Seed the corpus

Auto-seeds on first search, or explicitly:
```bash
./venv/bin/python redis_store.py        # seeds + runs a sample query
```

## Redis Cloud (ACTIVE)

The app is pointed at Redis Cloud via a single `REDIS_URL` in the app-local
`.env` (gitignored). `client.py` / `redis_store.py` auto-load `.env` (python-dotenv),
so no `source` is needed.

```
# .env (not committed)
REDIS_URL=redis://default:<password>@<host>:<port>
```

`get_client()` prefers `REDIS_URL`; if it's unset it falls back to
`REDIS_HOST`/`REDIS_PORT`/`REDIS_PASSWORD` (local Docker on 6380). To re-seed the
cloud index: `./venv/bin/python redis_store.py`. Redis Cloud includes RediSearch,
so the vector index works as-is. The local Docker container is now optional.
