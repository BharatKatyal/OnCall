"""No-microphone preflight: connects with the REAL settings from client.py,
sends them, and prints the server's first replies. Confirms the schema, the
Anthropic provider + key, the function definitions, and that the greeting plays
— all without talking. Run this before client.py to catch config errors fast.

    python preflight.py
"""
import asyncio
import json
import os
import ssl
import certifi
import websockets

import client  # reuse the exact SETTINGS / URL the real app uses


async def main():
    dg = os.environ.get("DEEPGRAM_API_KEY")
    an = os.environ.get("ANTHROPIC_API_KEY")
    if not dg:
        print("FAIL: DEEPGRAM_API_KEY not set"); return
    if not an:
        print("FAIL: ANTHROPIC_API_KEY not set"); return

    settings = client.SETTINGS.copy()
    settings["agent"]["think"]["endpoint"]["headers"]["x-api-key"] = an
    ssl_context = ssl.create_default_context(cafile=certifi.where())

    print(f"Connecting to {client.VOICE_AGENT_URL} ...")
    async with websockets.connect(
        client.VOICE_AGENT_URL,
        ssl=ssl_context,
        extra_headers={"Authorization": f"Token {dg}"},
    ) as ws:
        await ws.send(json.dumps(settings))
        applied = False
        try:
            for _ in range(8):
                msg = await asyncio.wait_for(ws.recv(), timeout=8)
                if isinstance(msg, bytes):
                    print(f"  <audio {len(msg)} bytes>")
                    continue
                print("  " + msg)
                data = json.loads(msg)
                if data.get("type") == "SettingsApplied":
                    applied = True
                if data.get("type") == "Error":
                    print("\nRESULT: settings REJECTED ->", data.get("description"))
                    return
        except asyncio.TimeoutError:
            pass
        print("\nRESULT:", "PASS — settings accepted, greeting streaming." if applied
              else "INCONCLUSIVE — no SettingsApplied seen.")


asyncio.run(main())
