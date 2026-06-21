import json
from datetime import datetime, timedelta
import asyncio
import k8s
from business_logic import (
    query_logs,
    get_metric,
    search_incident_memory,
    propose_fix,
    prepare_agent_filler_message,
    prepare_farewell_message
)


async def run_get_pods(params):
    """List pods (cluster-wide unhealthy view, or all pods in a namespace)."""
    namespace = params.get("namespace")
    all_pods = bool(params.get("all_pods", False))
    return await asyncio.to_thread(k8s.get_pods, namespace, all_pods)


async def run_describe_pod(params):
    """Root-cause one pod: container states/reasons + recent events."""
    name = params.get("name")
    if not name:
        return {"error": "name is required"}
    namespace = params.get("namespace", "default")
    return await asyncio.to_thread(k8s.describe_pod, name, namespace)


async def run_get_pod_logs(params):
    """Tail real logs for a pod."""
    name = params.get("name")
    if not name:
        return {"error": "name is required"}
    namespace = params.get("namespace", "default")
    lines = params.get("lines", 50)
    return await asyncio.to_thread(k8s.get_pod_logs, name, namespace, lines)


async def run_get_cluster_events(params):
    """Recent cluster Warning events — a fast 'what's wrong' scan."""
    namespace = params.get("namespace")
    return await asyncio.to_thread(k8s.get_events, namespace, True)

async def run_query_logs(params):
    """Query recent log entries for a service."""
    service = params.get("service")
    if not service:
        return {"error": "service is required"}
    minutes = params.get("minutes", 15)

    result = await query_logs(service, minutes)
    return result

async def run_get_metric(params):
    """Get the current value and trend for an infrastructure metric."""
    metric = params.get("metric")
    resource = params.get("resource")
    if not metric or not resource:
        return {"error": "metric and resource are required"}

    result = await get_metric(metric, resource)
    return result

async def run_search_incident_memory(params):
    """Semantic search over past incidents and runbooks."""
    query = params.get("query")
    if not query:
        return {"error": "query is required"}

    result = await search_incident_memory(query)
    return result

async def run_propose_fix(params):
    """Generate a proposed remediation for a diagnosed issue."""
    issue = params.get("issue")
    if not issue:
        return {"error": "issue is required"}
    based_on_incident = params.get("based_on_incident")

    result = await propose_fix(issue, based_on_incident)
    return result

async def agent_filler(websocket, params):
    """
    Handle agent filler messages while maintaining proper function call protocol.
    """
    result = await prepare_agent_filler_message(websocket, **params)
    return result

async def end_call(websocket, params):
    """
    End the conversation and close the connection.
    """
    farewell_type = params.get("farewell_type", "general")
    result = await prepare_farewell_message(websocket, farewell_type)
    return result

# Function definitions that will be sent to the Voice Agent API
FUNCTION_DEFINITIONS = [
    {
        "name": "agent_filler",
        "description": """Use this function to provide natural conversational filler before looking up information.
        ALWAYS call this function first with message_type='lookup' when you're about to investigate (query logs, pull a metric, or search incident memory).
        After calling this function, you MUST immediately follow up with the appropriate investigation function (e.g., search_incident_memory, query_logs, get_metric).""",
        "parameters": {
            "type": "object",
            "properties": {
                "message_type": {
                    "type": "string",
                    "description": "Type of filler message to use. Use 'lookup' when about to search for information.",
                    "enum": ["lookup", "general"]
                }
            },
            "required": ["message_type"]
        }
    },
    {
        "name": "query_logs",
        "description": """Query recent log entries for a service. Returns error and warning lines from the last N minutes.
        Use this when the engineer reports errors, crashes, or wants to know what a service is logging.
        ALWAYS pair investigation with search_incident_memory before giving a diagnosis.""",
        "parameters": {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "description": "Service name, e.g. 'gateway-logdb'"
                },
                "minutes": {
                    "type": "integer",
                    "description": "Lookback window in minutes. Defaults to 15 if not specified."
                }
            },
            "required": ["service"]
        }
    },
    {
        "name": "get_metric",
        "description": """Get the current value and recent trend for an infrastructure metric.
        Use this when the engineer wants numbers: connection counts, disk usage, CPU, etc.""",
        "parameters": {
            "type": "object",
            "properties": {
                "metric": {
                    "type": "string",
                    "description": "Metric name, e.g. 'db_connections', 'disk_usage', 'cpu'"
                },
                "resource": {
                    "type": "string",
                    "description": "Resource identifier the metric belongs to, e.g. a cluster or pod name"
                }
            },
            "required": ["metric", "resource"]
        }
    },
    {
        "name": "search_incident_memory",
        "description": """Semantic search over past incidents and runbooks. Returns the most relevant
        past incident with its resolution. THIS IS YOUR MOST VALUABLE TOOL: call it before every
        diagnosis. If it returns a relevant incident, lead with what happened last time and what fixed it.""",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language description of the current symptom"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "propose_fix",
        "description": """Generate a proposed remediation for a diagnosed issue. Returns the steps;
        does NOT execute them. Call this when the engineer asks you to act or fix something. Read the
        proposed steps back conversationally and make clear they require human approval — you propose,
        the human approves.""",
        "parameters": {
            "type": "object",
            "properties": {
                "issue": {
                    "type": "string",
                    "description": "The diagnosed problem to remediate"
                },
                "based_on_incident": {
                    "type": "string",
                    "description": "ID of the past incident this fix is modeled on, if any (e.g. 'INC-0412')"
                }
            },
            "required": ["issue"]
        }
    },
    {
        "name": "get_pods",
        "description": """Inspect the LIVE Kubernetes cluster (read-only). With no namespace,
        returns the pods that are currently NOT healthy across the whole cluster — your first
        stop for "what's broken right now". Pass a namespace to list every pod in it. Use this
        to find CrashLoopBackOff, Error, ImagePullBackOff, or not-ready pods.""",
        "parameters": {
            "type": "object",
            "properties": {
                "namespace": {
                    "type": "string",
                    "description": "Kubernetes namespace, e.g. 'replyagent-stage-ns'. Omit to scan the whole cluster for unhealthy pods."
                },
                "all_pods": {
                    "type": "boolean",
                    "description": "If true (with a namespace), include healthy pods too. Default false."
                }
            }
        }
    },
    {
        "name": "describe_pod",
        "description": """Root-cause a single pod on the LIVE cluster: container states, failure
        reasons, exit codes, restart counts, and recent Kubernetes events. Call this after get_pods
        to understand WHY a specific pod is failing.""",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Exact pod name from get_pods"},
                "namespace": {"type": "string", "description": "The pod's namespace"}
            },
            "required": ["name"]
        }
    },
    {
        "name": "get_pod_logs",
        "description": """Read the actual recent log lines from a pod on the LIVE cluster. For a
        crashed pod this automatically falls back to the previous (crashed) container's logs.""",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Exact pod name"},
                "namespace": {"type": "string", "description": "The pod's namespace"},
                "lines": {"type": "integer", "description": "How many trailing log lines to return. Default 50."}
            },
            "required": ["name"]
        }
    },
    {
        "name": "get_cluster_events",
        "description": """Recent Warning events from the LIVE cluster — failed probes, backoffs,
        evictions, scheduling failures. A fast cluster-wide 'what's going wrong' scan. Pass a
        namespace to scope it.""",
        "parameters": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Namespace to scope events to. Omit for cluster-wide."}
            }
        }
    },
    {
        "name": "end_call",
        "description": """End the conversation and close the connection. Call this function when:
        - User says goodbye, thank you, etc.
        - User indicates they're done ("that's all I need", "I'm all set", etc.)
        - User wants to end the conversation
        
        Examples of triggers:
        - "Thank you, bye!"
        - "That's all I needed, thanks"
        - "Have a good day"
        - "Goodbye"
        - "I'm done"
        
        Do not call this function if the user is just saying thanks but continuing the conversation.""",
        "parameters": {
            "type": "object",
            "properties": {
                "farewell_type": {
                    "type": "string",
                    "description": "Type of farewell to use in response",
                    "enum": ["thanks", "general", "help"]
                }
            },
            "required": ["farewell_type"]
        }
    }
]

# Map function names to their implementations
FUNCTION_MAP = {
    "query_logs": run_query_logs,
    "get_metric": run_get_metric,
    "search_incident_memory": run_search_incident_memory,
    "propose_fix": run_propose_fix,
    "get_pods": run_get_pods,
    "describe_pod": run_describe_pod,
    "get_pod_logs": run_get_pod_logs,
    "get_cluster_events": run_get_cluster_events,
    "agent_filler": agent_filler,
    "end_call": end_call
}