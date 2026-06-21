import pyaudio
import asyncio
import websockets
import ssl
import certifi
import os
import json
import threading
import janus
import queue
import sys
import time
from datetime import datetime
from functions import FUNCTION_DEFINITIONS, FUNCTION_MAP
import logging

# Load env from a local .env so `python client.py` works without `source`.
# Already-exported shell vars take precedence; missing python-dotenv is fine.
try:
    from dotenv import load_dotenv
    _HERE = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(_HERE, ".env"))                 # app-local: REDIS_URL etc.
    load_dotenv(os.path.join(_HERE, "..", "..", ".env"))     # workspace: API keys
except ImportError:
    pass

class ColorFormatter(logging.Formatter):
    """Custom formatter to color-code log messages based on their content."""
    
    # ANSI escape codes for colors - using accessible palette
    COLORS = {
        'RESET': '\033[0m',
        'WHITE': '\033[38;5;231m',    # Default text color
        'BLUE': '\033[38;5;116m',    # User/STT messages
        'GREEN': '\033[38;5;114m',    # Agent speaking/TTS
        'VIOLET': '\033[38;5;183m',   # Function calls
        'YELLOW': '\033[38;5;186m',   # Latency info
    }
    
    def format(self, record):
        # Default format string
        format_str = '%(asctime)s.%(msecs)03d %(levelname)s: %(message)s'
        
        # Default to white
        color = self.COLORS['WHITE']
        
        msg = str(record.msg).lower()
        
        # Check for JSON content
        if "server:" in msg and "{" in msg:
            try:
                # Extract the JSON part
                json_str = msg[msg.find("{"):msg.rfind("}") + 1]
                data = json.loads(json_str)
                
                # User/STT related messages
                if (data.get("type") in ["userstartedspeaking", "endofthought"] or
                    (data.get("type") == "conversationtext" and data.get("role") == "user")):
                    color = self.COLORS['BLUE']
                
                # Agent speaking/TTS related messages
                elif (data.get("type") in ["agentstartedspeaking", "agentaudiodone"] or
                      (data.get("type") == "conversationtext" and data.get("role") == "assistant")):
                    color = self.COLORS['GREEN']
                
                # Agent thinking/function calling
                elif data.get("type") in ["functioncalling", "functioncallrequest"]:
                    color = self.COLORS['VIOLET']
                
            except (json.JSONDecodeError, KeyError):
                pass
        
        # Non-JSON messages
        else:
            if any(phrase in msg for phrase in ["function response", "parameters", "function call"]):
                color = self.COLORS['VIOLET']
            elif "injectagentmessage" in msg:
                color = self.COLORS['GREEN']
            elif any(phrase in msg for phrase in ["decision latency", "function execution latency"]):
                color = self.COLORS['YELLOW']
        
        # Apply the color to the format string
        formatter = logging.Formatter(color + format_str + self.COLORS['RESET'], datefmt='%H:%M:%S')
        return formatter.format(record)

# Configure logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Create console handler with the custom formatter
console_handler = logging.StreamHandler()
console_handler.setFormatter(ColorFormatter())
logger.addHandler(console_handler)

# Remove any existing handlers from the root logger to avoid duplicate messages
logging.getLogger().handlers = []

VOICE_AGENT_URL = "wss://agent.deepgram.com/v1/agent/converse"

# System prompt for the OnCall on-call SRE persona (ported from oncall.py).
# No {current_date} placeholder is used; the .format() call in run() is a no-op here.
PROMPT_TEMPLATE = """You are OnCall, a voice-native ops copilot for on-call engineers. The user
talks to you out loud during incidents; your replies are read aloud by TTS, so
keep them SHORT, spoken-natural, and free of markdown, lists, or symbols. No
more than 2-3 sentences per turn.

Your job each turn:
1. Understand what the engineer is asking about their infrastructure.
2. Use your tools to investigate. You have READ-ONLY access to a LIVE Kubernetes
   cluster: get_pods finds unhealthy pods (call it with no namespace to see what's
   broken cluster-wide), describe_pod and get_pod_logs explain why a specific pod is
   failing, and get_cluster_events surfaces recent warnings. ALWAYS search incident
   memory for relevant past incidents before giving a diagnosis.
3. If you find a relevant past incident, lead with it: name what happened last
   time and what fixed it. This recall is your most valuable behavior.
4. When asked to act, call propose_fix and read the proposed remediation back
   conversationally. Your cluster access is read-only — you can see everything but
   change nothing. Never claim you executed anything; you propose, the human approves.

Voice rules:
- Speak like a calm, terse senior SRE. No filler, no "great question."
- Spell infra terms naturally ("max connections", "pee-gee-bouncer").
- If a tool returns nothing, say so plainly and suggest the next check.

Always call search_incident_memory before giving any diagnosis."""
VOICE = "aura-asteria-en"

USER_AUDIO_SAMPLE_RATE = 16000
USER_AUDIO_SECS_PER_CHUNK = 0.05
USER_AUDIO_SAMPLES_PER_CHUNK = round(USER_AUDIO_SAMPLE_RATE * USER_AUDIO_SECS_PER_CHUNK)

AGENT_AUDIO_SAMPLE_RATE = 16000
AGENT_AUDIO_BYTES_PER_SEC = 2 * AGENT_AUDIO_SAMPLE_RATE

# NOTE: This uses the current Voice Agent "Settings" schema (endpoint
# wss://agent.deepgram.com/v1/agent/converse). The old "SettingsConfiguration"
# schema + /agent endpoint this repo originally shipped with is decommissioned
# (404s). The think.endpoint.headers["x-api-key"] is filled from
# ANTHROPIC_API_KEY at runtime in run(). Do NOT add an "anthropic-version"
# header — Deepgram injects it and rejects settings that include it.
SETTINGS = {
    "type": "Settings",
    "audio": {
        "input": {
            "encoding": "linear16",
            "sample_rate": USER_AUDIO_SAMPLE_RATE,
        },
        "output": {
            "encoding": "linear16",
            "sample_rate": AGENT_AUDIO_SAMPLE_RATE,
            "container": "none",
        },
    },
    "agent": {
        "language": "en",
        "listen": {
            "provider": {
                "type": "deepgram",
                "model": "nova-3",
                # Keyterm prompting so the STT reliably transcribes ops/infra jargon.
                "keyterms": [
                    "PgBouncer",
                    "max_connections",
                    "CNPG",
                    "PVC",
                    "CrashLoopBackOff",
                    "Lambda",
                    "Cognito",
                    "ARN",
                ],
            },
        },
        "think": {
            "provider": {"type": "anthropic", "model": "claude-sonnet-4-6"},
            "endpoint": {
                "url": "https://api.anthropic.com/v1/messages",
                "headers": {"x-api-key": ""},  # filled from ANTHROPIC_API_KEY in run()
            },
            "prompt": PROMPT_TEMPLATE,
            "functions": FUNCTION_DEFINITIONS,
        },
        "speak": {"provider": {"type": "deepgram", "model": VOICE}},
        "greeting": "OnCall here. What's breaking?",
    },
}

mic_audio_queue = asyncio.Queue()


def callback(input_data, frame_count, time_info, status_flag):
    mic_audio_queue.put_nowait(input_data)
    return (input_data, pyaudio.paContinue)


async def run():
    dg_api_key = os.environ.get("DEEPGRAM_API_KEY")
    if dg_api_key is None:
        logger.error("DEEPGRAM_API_KEY env var not present")
        return

    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_api_key:
        logger.error("ANTHROPIC_API_KEY env var not present (required for the Anthropic think provider)")
        return

    # Warm the embedding model + Redis index in the background so the first
    # spoken query doesn't pay model-load latency.
    import redis_store
    threading.Thread(target=redis_store.warmup, daemon=True).start()

    # Inject the Anthropic BYO-LLM key into the think endpoint headers.
    settings = SETTINGS.copy()
    settings["agent"]["think"]["endpoint"]["headers"]["x-api-key"] = anthropic_api_key

    # python.org builds on macOS don't use the system trust store, so point
    # OpenSSL at certifi's CA bundle for the wss handshake.
    ssl_context = ssl.create_default_context(cafile=certifi.where())

    async with websockets.connect(
        VOICE_AGENT_URL,
        ssl=ssl_context,
        extra_headers={"Authorization": f"Token {dg_api_key}"},
    ) as ws:

        async def microphone():
            audio = pyaudio.PyAudio()
            stream = audio.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=USER_AUDIO_SAMPLE_RATE,
                input=True,
                frames_per_buffer=USER_AUDIO_SAMPLES_PER_CHUNK,
                stream_callback=callback,
            )

            stream.start_stream()

            while stream.is_active():
                await asyncio.sleep(0.1)

            stream.stop_stream()
            stream.close()

        async def sender(ws):
            await ws.send(json.dumps(settings))

            try:
                while True:
                    data = await mic_audio_queue.get()
                    await ws.send(data)

            except Exception as e:
                logger.error("Error while sending: " + str(e))
                raise

        async def receiver(ws):
            try:
                speaker = Speaker()
                last_user_message = None
                last_function_response_time = None
                in_function_chain = False  # Flag to track if we're in a chain of function calls
                
                with speaker:
                    async for message in ws:
                        # Print raw message for debugging, but only if it's not binary audio data
                        if isinstance(message, str):
                            logger.info(f"Server: {message}")                     
                        
                        if isinstance(message, str):
                            message_json = json.loads(message)
                            message_type = message_json.get("type")
                            current_time = time.time()
                            
                            if message_type == "UserStartedSpeaking":
                                speaker.stop()
                                continue
                            # Track when user speaks
                            if message_type == "ConversationText" and message_json.get("role") == "user":
                                last_user_message = current_time
                                in_function_chain = False  # Reset chain flag when user speaks
                            
                            # Track when assistant speaks to reset chain flag
                            elif message_type == "ConversationText" and message_json.get("role") == "assistant":
                                in_function_chain = False  # Reset chain flag when assistant speaks to user
                            
                            elif message_type == "FunctionCalling":
                                # Determine which timestamp to use for latency calculation
                                if in_function_chain and last_function_response_time:
                                    # If we're in a chain, measure from last function response
                                    latency = current_time - last_function_response_time
                                    logger.info(f"LLM Decision Latency (chain): {latency:.3f}s")
                                elif last_user_message:
                                    # If it's the first function call, measure from last user message
                                    latency = current_time - last_user_message
                                    logger.info(f"LLM Decision Latency (initial): {latency:.3f}s")
                                    in_function_chain = True  # Start a chain
                            
                            elif message_type == "FunctionCallRequest":
                                # New schema: a single request may carry multiple calls in a
                                # "functions" array, and each call's arguments is a JSON string.
                                for fn in message_json.get("functions", []):
                                    function_name = fn.get("name")
                                    function_call_id = fn.get("id")
                                    # Server-side functions are executed by Deepgram, not us.
                                    if not fn.get("client_side", True):
                                        continue
                                    try:
                                        parameters = json.loads(fn.get("arguments") or "{}")
                                    except (json.JSONDecodeError, TypeError):
                                        parameters = {}

                                    logger.info(f"Function call received: {function_name}")
                                    logger.info(f"Parameters: {parameters}")

                                    start_time = time.time()
                                    try:
                                        func = FUNCTION_MAP.get(function_name)
                                        if not func:
                                            raise ValueError(f"Function {function_name} not found")

                                        # Special handling for functions that need websocket
                                        if function_name in ["agent_filler", "end_call"]:
                                            result = await func(ws, parameters)

                                            if function_name == "agent_filler":
                                                # Extract messages
                                                inject_message = result["inject_message"]
                                                function_response = result["function_response"]

                                                # First send the function response
                                                response = {
                                                    "type": "FunctionCallResponse",
                                                    "id": function_call_id,
                                                    "name": function_name,
                                                    "content": json.dumps(function_response)
                                                }
                                                await ws.send(json.dumps(response))
                                                logger.info(f"Function response sent: {json.dumps(function_response)}")

                                                # Update the last function response time
                                                last_function_response_time = time.time()
                                                # Then just inject the message and continue
                                                await inject_agent_message(ws, inject_message)
                                                continue

                                            elif function_name == "end_call":
                                                # Extract messages
                                                inject_message = result["inject_message"]
                                                function_response = result["function_response"]
                                                close_message = result["close_message"]

                                                # First send the function response
                                                response = {
                                                    "type": "FunctionCallResponse",
                                                    "id": function_call_id,
                                                    "name": function_name,
                                                    "content": json.dumps(function_response)
                                                }
                                                await ws.send(json.dumps(response))
                                                logger.info(f"Function response sent: {json.dumps(function_response)}")

                                                # Update the last function response time
                                                last_function_response_time = time.time()

                                                # Then wait for farewell sequence to complete
                                                await wait_for_farewell_completion(ws, speaker, inject_message)

                                                # Finally send the close message and exit
                                                logger.info(f"Sending ws close message")
                                                await close_websocket_with_timeout(ws)
                                                os._exit(0)  # Clean exit without traceback
                                        else:
                                            result = await func(parameters)

                                    except Exception as e:
                                        logger.error(f"Error executing function: {str(e)}")
                                        result = {"error": str(e)}

                                    execution_time = time.time() - start_time
                                    logger.info(f"Function Execution Latency: {execution_time:.3f}s")

                                    # Send the response back with stringified output (non-filler functions)
                                    response = {
                                        "type": "FunctionCallResponse",
                                        "id": function_call_id,
                                        "name": function_name,
                                        "content": json.dumps(result)
                                    }
                                    await ws.send(json.dumps(response))
                                    logger.info(f"Function response sent: {json.dumps(result)}")

                                    # Update the last function response time
                                    last_function_response_time = time.time()

                            # Handle different message types
                            message_type = message_json.get("type")
                            
                            if message_type == "Welcome":
                                logger.info(f"Connected with session ID: {message_json.get('session_id')}")
                                continue
                            
                            elif message_type == "CloseConnection":
                                logger.info("Closing connection...")
                                await ws.close()
                                return  # Exit the function to end the script
        
                        elif isinstance(message, bytes):
                            await speaker.play(message)
        
            except Exception as e:
                logger.error(f"Receiver encountered an error: {e}")
                import traceback
                traceback.print_exc()

        await asyncio.wait(
            [
                asyncio.ensure_future(microphone()),
                asyncio.ensure_future(sender(ws)),
                asyncio.ensure_future(receiver(ws)),
            ]
        )


def main():
    asyncio.run(run())


def _play(audio_out, stream, stop):
    while not stop.is_set():
        try:
            # Janus sync queue mimics the API of queue.Queue, and async queue mimics the API of
            # asyncio.Queue. So for this line check these docs:
            # https://docs.python.org/3/library/queue.html#queue.Queue.get.
            #
            # The timeout of 0.05 is to prevent this line from going into an uninterruptible wait,
            # which can interfere with shutting down the program on some systems.
            data = audio_out.sync_q.get(True, 0.05)

            # In PyAudio's "blocking mode," the `write` function will block until playback is
            # finished. This is why we can stop playback very quickly by simply stopping this loop;
            # there is never more than 1 chunk of audio awaiting playback inside PyAudio.
            # Read more: https://people.csail.mit.edu/hubert/pyaudio/docs/#example-blocking-mode-audio-i-o
            stream.write(data)

        except queue.Empty:
            pass


class Speaker:
    def __init__(self):
        self._queue = None
        self._stream = None
        self._thread = None
        self._stop = None

    def __enter__(self):
        audio = pyaudio.PyAudio()
        self._stream = audio.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=AGENT_AUDIO_SAMPLE_RATE,
            input=False,
            output=True,
        )
        self._queue = janus.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=_play, args=(self._queue, self._stream, self._stop), daemon=True
        )
        self._thread.start()

    def __exit__(self, exc_type, exc_value, traceback):
        self._stop.set()
        self._thread.join()
        self._stream.close()
        self._stream = None
        self._queue = None
        self._thread = None
        self._stop = None

    async def play(self, data):
        return await self._queue.async_q.put(data)

    def stop(self):
        if self._queue and self._queue.async_q:
            while not self._queue.async_q.empty():
                try:
                    self._queue.async_q.get_nowait()
                except janus.QueueEmpty:
                    break


async def inject_agent_message(ws, inject_message):
    """Simple helper to inject an agent message."""
    logger.info(f"Sending InjectAgentMessage: {json.dumps(inject_message)}")
    await ws.send(json.dumps(inject_message))

async def close_websocket_with_timeout(ws, timeout=5):
    """Close websocket with timeout to avoid hanging if no close frame is received."""
    try:
        await asyncio.wait_for(ws.close(), timeout=timeout)
    except Exception as e:
        logger.error(f"Error during websocket closure: {e}")

async def wait_for_farewell_completion(ws, speaker, inject_message):
    """Wait for the farewell message to be spoken completely by the agent."""
    # Send the farewell message
    await inject_agent_message(ws, inject_message)
    
    # First wait for either AgentStartedSpeaking or matching ConversationText
    speaking_started = False
    while not speaking_started:
        message = await ws.recv()
        if isinstance(message, bytes):
            await speaker.play(message)
            continue
            
        try:
            message_json = json.loads(message)
            logger.info(f"Server: {message}")
            if (message_json.get("type") == "AgentStartedSpeaking" or 
                (message_json.get("type") == "ConversationText" and 
                 message_json.get("role") == "assistant" and 
                 message_json.get("content") == inject_message["message"])):
                speaking_started = True
        except json.JSONDecodeError:
            continue
    
    # Then wait for AgentAudioDone
    audio_done = False
    while not audio_done:
        message = await ws.recv()
        if isinstance(message, bytes):
            await speaker.play(message)
            continue
            
        try:
            message_json = json.loads(message)
            logger.info(f"Server: {message}")
            if message_json.get("type") == "AgentAudioDone":
                audio_done = True
        except json.JSONDecodeError:
            continue
            
    # Give audio time to play completely
    await asyncio.sleep(3.5)


if __name__ == "__main__":
    sys.exit(main() or 0)
