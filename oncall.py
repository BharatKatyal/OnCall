import json
import anthropic

client = anthropic.Anthropic()

SYSTEM_PROMPT = """You are OnCall, a voice-native ops copilot for on-call engineers. The user
talks to you out loud during incidents; your replies are read aloud by TTS, so
keep them SHORT, spoken-natural, and free of markdown, lists, or symbols. No
more than 2-3 sentences per turn.

Your job each turn:
1. Understand what the engineer is asking about their infrastructure.
2. Use your tools to investigate: check logs, pull metrics, and ALWAYS search
   incident memory for relevant past incidents before answering.
3. If you find a relevant past incident, lead with it: name what happened last
   time and what fixed it. This recall is your most valuable behavior.
4. When asked to act, call propose_fix and read the proposed remediation back
   conversationally. Never claim you executed anything — you propose, the human
   approves.

Voice rules:
- Speak like a calm, terse senior SRE. No filler, no "great question."
- Spell infra terms naturally ("max connections", "pee-gee-bouncer").
- If a tool returns nothing, say so plainly and suggest the next check.

Always call search_incident_memory before giving any diagnosis."""

TOOLS = [
    {
        "name": "query_logs",
        "description": "Query recent log entries for a service. Returns error and warning lines from the last N minutes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Service name, e.g. 'gateway-logdb'"},
                "minutes": {"type": "integer", "description": "Lookback window in minutes"}
            },
            "required": ["service"]
        }
    },
    {
        "name": "get_metric",
        "description": "Get the current value and recent trend for an infrastructure metric.",
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {"type": "string", "description": "e.g. 'db_connections', 'disk_usage', 'cpu'"},
                "resource": {"type": "string", "description": "Resource identifier"}
            },
            "required": ["metric", "resource"]
        }
    },
    {
        "name": "search_incident_memory",
        "description": "Semantic search over past incidents and runbooks in Redis. Returns the most relevant past incident with its resolution. Call this before every diagnosis.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language description of the current symptom"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "propose_fix",
        "description": "Generate a proposed remediation for a diagnosed issue. Returns the steps; does NOT execute them.",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue": {"type": "string", "description": "The diagnosed problem"},
                "based_on_incident": {"type": "string", "description": "ID of the past incident this fix is modeled on, if any"}
            },
            "required": ["issue"]
        }
    }
]

# --- Mock implementations (real CNPG numbers baked in) ---

def query_logs(service, minutes=15):
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

def get_metric(metric, resource):
    if metric == "db_connections":
        return {"metric": metric, "resource": resource,
                "current": 98, "limit": 100, "trend": "rising", "unit": "connections"}
    if metric == "disk_usage":
        return {"metric": metric, "resource": resource,
                "current": 9.4, "limit": 10, "trend": "rising", "unit": "Gi"}
    return {"metric": metric, "resource": resource, "current": None}

def search_incident_memory(query):
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

def propose_fix(issue, based_on_incident=None):
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

DISPATCH = {
    "query_logs": query_logs,
    "get_metric": get_metric,
    "search_incident_memory": search_incident_memory,
    "propose_fix": propose_fix,
}

def run_turn(user_text, history):
    history.append({"role": "user", "content": user_text})
    while True:
        resp = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=history,
        )
        history.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason != "tool_use":
            text = "".join(b.text for b in resp.content if b.type == "text")
            return text, history

        results = []
        for block in resp.content:
            if block.type == "tool_use":
                print(f"  [tool] {block.name}({block.input})")
                out = DISPATCH[block.name](**block.input)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(out),
                })
        history.append({"role": "user", "content": results})

if __name__ == "__main__":
    hist = []
    print("OnCall — type a question, Ctrl-C to quit.\n")
    while True:
        try:
            q = input("you> ")
        except (EOFError, KeyboardInterrupt):
            break
        reply, hist = run_turn(q, hist)
        print(f"oncall> {reply}\n")