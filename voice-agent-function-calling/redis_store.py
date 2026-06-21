"""Redis-backed incident memory with vector (semantic) search.

This is the "memory plane" the OnCall agent searches before every diagnosis.
Incidents are stored as Redis hashes under the `incident:` prefix and indexed
with a RediSearch vector field; queries are embedded with a small local
model2vec model (CPU, no torch) and retrieved by cosine KNN.

Connection comes from env (REDIS_HOST / REDIS_PORT / REDIS_PASSWORD), defaulting
to the local Docker Redis Stack on port 6380. To move to Redis Cloud, just set
those env vars — no code change.
"""
import os

# Keep the demo console clean: the embedding model is already cached locally, so
# skip the HuggingFace hub check, its progress bars, and tokenizer thread spam
# (the latter is what emits the "leaked semaphore" warning at shutdown).
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# Load config from .env (app-local first, then the workspace-root .env that holds
# the API keys). Already-exported shell vars win; missing python-dotenv is fine.
try:
    from dotenv import load_dotenv
    _HERE = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(_HERE, ".env"))
    load_dotenv(os.path.join(_HERE, "..", "..", ".env"))
except ImportError:
    pass

import json
import numpy as np
import redis
from redis.commands.search.field import TextField, NumericField, VectorField
from redis.commands.search.index_definition import IndexDefinition, IndexType
from redis.commands.search.query import Query

INDEX_NAME = "incidents_idx"
KEY_PREFIX = "incident:"
EMBED_MODEL = "minishlab/potion-base-8M"  # 256-dim static embeddings, CPU-only
VECTOR_DIM = 256

# Semantic cache: a second vector index over past queries. A new query whose
# embedding is within CACHE_SIM_THRESHOLD cosine similarity of a cached one
# returns the stored result instantly, skipping the incident KNN.
CACHE_INDEX = "qcache_idx"
CACHE_PREFIX = "qcache:"
# model2vec static-embedding cosine runs lower than transformer models:
# same-intent rephrases land ~0.57-0.91, different incidents <=0.10. 0.50 catches
# rephrases reliably with a wide safety margin against false hits.
CACHE_SIM_THRESHOLD = 0.50

# --- Seed corpus -------------------------------------------------------------
# INC-0412 is the hero incident and its fields are kept VERBATIM from oncall.py.
# The others give the vector index real semantic competition so retrieving
# INC-0412 for a "too many clients" query demonstrates genuine search.
INCIDENTS = [
    {
        "id": "INC-0412",
        "title": "replyagent-gateway-logdb connection exhaustion",
        "symptom": "Postgres hit max_connections, app threw 'too many clients'",
        "resolution": "Raised max_connections to 200, deployed PgBouncer Pooler "
                      "to multiplex connections, expanded PVC 10Gi to 20Gi after "
                      "disk-full CrashLoopBackOff.",
        "resolved_in_minutes": 38,
    },
    {
        "id": "INC-0405",
        "title": "gateway-logdb PVC disk full CrashLoopBackOff",
        "symptom": "Pod stuck in CrashLoopBackOff, logs showed no space left on device",
        "resolution": "Expanded the PVC from 10Gi to 20Gi and restarted the pod; "
                      "added a disk-usage alert at 80%.",
        "resolved_in_minutes": 22,
    },
    {
        "id": "INC-0391",
        "title": "auth-api Lambda timeouts after Cognito token refresh",
        "symptom": "API Gateway 504s, Lambda duration spiking past timeout on cold start",
        "resolution": "Raised Lambda memory and timeout, enabled provisioned "
                      "concurrency, cached Cognito JWKS to cut per-request latency.",
        "resolved_in_minutes": 51,
    },
    {
        "id": "INC-0420",
        "title": "CNPG cluster failover with replication lag",
        "symptom": "Primary Postgres failed over, replicas behind, writes rejected briefly",
        "resolution": "Promoted the healthiest replica, tuned max_wal_size and "
                      "synchronous_commit, monitored CNPG lag back to zero.",
        "resolved_in_minutes": 44,
    },
    {
        "id": "INC-0377",
        "title": "S3 access denied from worker role ARN",
        "symptom": "Background jobs failing with AccessDenied reading from the bucket",
        "resolution": "Fixed the IAM policy on the worker role ARN to include "
                      "s3:GetObject for the correct bucket prefix.",
        "resolved_in_minutes": 17,
    },
]

_model = None
_client = None


def get_client():
    global _client
    if _client is None:
        url = os.environ.get("REDIS_URL")
        if url:
            # Cloud (or any) Redis via a single connection string, e.g.
            # redis://default:<password>@host:port
            _client = redis.from_url(url, decode_responses=True)
        else:
            _client = redis.Redis(
                host=os.environ.get("REDIS_HOST", "localhost"),
                port=int(os.environ.get("REDIS_PORT", "6380")),
                password=os.environ.get("REDIS_PASSWORD") or None,
                decode_responses=True,
            )
    return _client


def get_model():
    global _model
    if _model is None:
        from model2vec import StaticModel
        _model = StaticModel.from_pretrained(EMBED_MODEL)
    return _model


def embed(text):
    """Return a float32 little-endian byte string for the given text."""
    vec = get_model().encode([text])[0].astype(np.float32)
    return vec.tobytes()


def _doc_text(inc):
    return f"{inc['title']}. {inc['symptom']} {inc['resolution']}"


def ensure_index():
    """Create the RediSearch vector index if it doesn't already exist."""
    r = get_client()
    try:
        r.ft(INDEX_NAME).info()
        return  # already exists
    except redis.ResponseError:
        pass
    schema = (
        TextField("id"),
        TextField("title"),
        TextField("symptom"),
        TextField("resolution"),
        NumericField("resolved_in_minutes"),
        VectorField(
            "embedding",
            "FLAT",
            {"TYPE": "FLOAT32", "DIM": VECTOR_DIM, "DISTANCE_METRIC": "COSINE"},
        ),
    )
    r.ft(INDEX_NAME).create_index(
        schema,
        definition=IndexDefinition(prefix=[KEY_PREFIX], index_type=IndexType.HASH),
    )


def ensure_cache_index():
    """Create the semantic-cache vector index if it doesn't already exist."""
    r = get_client()
    try:
        r.ft(CACHE_INDEX).info()
        return
    except redis.ResponseError:
        pass
    schema = (
        TextField("query"),
        TextField("result"),  # JSON-encoded search() result
        VectorField(
            "embedding",
            "FLAT",
            {"TYPE": "FLOAT32", "DIM": VECTOR_DIM, "DISTANCE_METRIC": "COSINE"},
        ),
    )
    r.ft(CACHE_INDEX).create_index(
        schema,
        definition=IndexDefinition(prefix=[CACHE_PREFIX], index_type=IndexType.HASH),
    )


def _cache_get(vec_bytes):
    """Return (result_dict, similarity) for a near-duplicate past query, or None."""
    r = get_client()
    q = (
        Query("*=>[KNN 1 @embedding $vec AS score]")
        .sort_by("score")
        .return_fields("result", "score")
        .dialect(2)
    )
    res = r.ft(CACHE_INDEX).search(q, query_params={"vec": vec_bytes})
    if not res.docs:
        return None
    sim = round(1.0 - float(res.docs[0].score), 2)
    if sim < CACHE_SIM_THRESHOLD:
        return None
    return json.loads(res.docs[0].result), sim


def _cache_put(query, vec_bytes, result):
    r = get_client()
    # Key by a stable hash of the query text so re-asking overwrites cleanly.
    key = CACHE_PREFIX + str(abs(hash(query)))
    r.hset(key, mapping={
        "query": query,
        "result": json.dumps(result),
        "embedding": vec_bytes,
    })


def seed(force=False):
    """Insert the incident corpus. Idempotent unless force=True."""
    r = get_client()
    ensure_index()
    for inc in INCIDENTS:
        key = KEY_PREFIX + inc["id"]
        if not force and r.exists(key):
            continue
        r.hset(key, mapping={
            "id": inc["id"],
            "title": inc["title"],
            "symptom": inc["symptom"],
            "resolution": inc["resolution"],
            "resolved_in_minutes": inc["resolved_in_minutes"],
            "embedding": embed(_doc_text(inc)),
        })
    return len(INCIDENTS)


def bootstrap():
    """Ensure the index exists and the corpus is loaded (safe to call repeatedly)."""
    ensure_index()
    ensure_cache_index()
    r = get_client()
    if int(r.ft(INDEX_NAME).info()["num_docs"]) == 0:
        seed()


def warmup():
    """Load the embedding model and ensure the index/corpus exist, so the first
    real query pays no model-load latency. Safe to call from a background thread."""
    try:
        bootstrap()
        get_model()
    except Exception as e:
        print(f"  [redis_store] warmup skipped: {e}")


def search(query, k=1):
    """Vector KNN over incident memory, fronted by a semantic cache.

    Returns {'match': {...}, 'similarity': float, 'cached': bool} for the top hit,
    or None if the corpus is empty. A query semantically close to a previous one
    short-circuits to the cached result.
    """
    bootstrap()
    r = get_client()
    vec = embed(query)

    cached = _cache_get(vec)
    if cached is not None:
        result, cache_sim = cached
        print(f"  [redis_store] semantic CACHE HIT ({cache_sim}) for {query!r}")
        result["cached"] = True
        return result

    q = (
        Query(f"*=>[KNN {k} @embedding $vec AS score]")
        .sort_by("score")
        .return_fields("title", "symptom", "resolution",
                       "resolved_in_minutes", "score")
        .dialect(2)
    )
    res = r.ft(INDEX_NAME).search(q, query_params={"vec": vec})
    if not res.docs:
        return None
    doc = res.docs[0]
    # COSINE distance is 1 - cosine_similarity; convert back to a 0..1 similarity.
    similarity = round(1.0 - float(doc.score), 2)
    result = {
        "match": {
            # doc.id is the Redis key, e.g. "incident:INC-0412"; strip the prefix.
            "id": doc.id.removeprefix(KEY_PREFIX),
            "title": doc.title,
            "symptom": doc.symptom,
            "resolution": doc.resolution,
            "resolved_in_minutes": int(float(doc.resolved_in_minutes)),
        },
        "similarity": similarity,
    }
    _cache_put(query, vec, result)
    print(f"  [redis_store] cache miss -> matched {result['match']['id']}, cached for next time")
    result["cached"] = False
    return result


if __name__ == "__main__":
    n = seed(force=True)
    print(f"Seeded {n} incidents into Redis index '{INDEX_NAME}'.")
    demo = search("postgres too many clients, connection limit")
    print("Top match for 'too many clients':", demo["match"]["id"], demo["similarity"])
