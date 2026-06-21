import asyncio
import json
from config import ARTIFICIAL_DELAY

async def simulate_delay(delay_type):
    """Simulate processing delay based on operation type."""
    await asyncio.sleep(ARTIFICIAL_DELAY[delay_type])

# --- Ops-incident business logic (mock implementations, real CNPG numbers baked in) ---
# Ported verbatim from oncall.py. The INC-0412 / gateway-logdb / max_connections=200 /
# PgBouncer / PVC 10Gi->20Gi details are real and must stay exactly as-is.

async def query_logs(service, minutes=15):
    """Query recent log entries for a service."""
    await simulate_delay("database")
    if "logdb" in service or "gateway" in service:
        return {
            "service": service,
            "errors": [
                "FATAL: sorry, too many clients already",
                "remaining connection slots reserved for superuser",
            ],
            "warning_count": 47,
        }
    return {"service": service, "errors": [], "warning_count": 0}

async def get_metric(metric, resource):
    """Get the current value and recent trend for an infrastructure metric."""
    await simulate_delay("database")
    if metric == "db_connections":
        return {"metric": metric, "resource": resource,
                "current": 98, "limit": 100, "trend": "rising", "unit": "connections"}
    if metric == "disk_usage":
        return {"metric": metric, "resource": resource,
                "current": 9.4, "limit": 10, "trend": "rising", "unit": "Gi"}
    return {"metric": metric, "resource": resource, "current": None}

async def search_incident_memory(query):
    """Semantic (vector) search over past incidents stored in Redis.

    Uses redis_store (RediSearch KNN over model2vec embeddings). Falls back to the
    canonical INC-0412 record if Redis is unavailable, so the demo never breaks.
    The sync Redis/embedding work runs in a thread to keep the event loop free.
    """
    await simulate_delay("database")
    try:
        import redis_store
        result = await asyncio.to_thread(redis_store.search, query)
        if result:
            return result
    except Exception as e:
        print(f"  [redis_store] vector search unavailable, using fallback: {e}")
    # Fallback: the verbatim hero incident (kept identical to the seeded INC-0412).
    return {
        "match": {
            "id": "INC-0412",
            "title": "replyagent-gateway-logdb connection exhaustion",
            "symptom": "Postgres hit max_connections, app threw 'too many clients'",
            "resolution": "Raised max_connections to 200, deployed PgBouncer Pooler "
                          "to multiplex connections, expanded PVC 10Gi to 20Gi after "
                          "disk-full CrashLoopBackOff.",
            "resolved_in_minutes": 38,
        },
        "similarity": 0.91,
    }

async def propose_fix(issue, based_on_incident=None):
    """Generate a proposed remediation for a diagnosed issue. Does NOT execute it."""
    await simulate_delay("database")
    return {
        "issue": issue,
        "modeled_on": based_on_incident,
        "steps": [
            "Patch the CNPG cluster: set max_connections to 200.",
            "Deploy a PgBouncer Pooler to multiplex connections.",
            "Pre-emptively expand the PVC from 10Gi to 20Gi to avoid disk-full crashloop.",
        ],
        "requires_human_approval": True,
    }

async def prepare_agent_filler_message(websocket, message_type):
    """
    Handle agent filler messages while maintaining proper function call protocol.
    Returns a simple confirmation first, then sends the actual message to the client.
    """
    # First prepare the result that will be the function call response
    result = {"status": "queued", "message_type": message_type}
    
    # Prepare the inject message but don't send it yet
    if message_type == "lookup":
        inject_message = {
            "type": "InjectAgentMessage",
            "message": "Let me look that up for you..."
        }
    else:
        inject_message = {
            "type": "InjectAgentMessage",
            "message": "One moment please..."
        }
    
    # Return the result first - this becomes the function call response
    # The caller can then send the inject message after handling the function response
    return {
        "function_response": result,
        "inject_message": inject_message
    }

async def prepare_farewell_message(websocket, farewell_type):
    """End the conversation with an appropriate farewell message and close the connection."""
    # Prepare farewell message based on type
    if farewell_type == "thanks":
        message = "Thank you for calling! Have a great day!"
    elif farewell_type == "help":
        message = "I'm glad I could help! Have a wonderful day!"
    else:  # general
        message = "Goodbye! Have a nice day!"
    
    # Prepare messages but don't send them
    inject_message = {
        "type": "InjectAgentMessage",
        "message": message
    }
    
    close_message = {
        "type": "close"
    }
    
    # Return both messages to be sent in correct order by the caller
    return {
        "function_response": {"status": "closing", "message": message},
        "inject_message": inject_message,
        "close_message": close_message
    }

