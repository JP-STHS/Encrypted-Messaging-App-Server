# =============================================================================
# CYBERAPP  —  RELAY SERVER  (Render-compatible)
# =============================================================================
# Fix: Render's health checker sends HTTP HEAD/GET requests to verify the
# service is alive. A pure WebSocket server rejects these, spamming errors.
# Solution: use aiohttp which handles both HTTP health probes AND WebSocket
# upgrades on the same port cleanly.
#
# ENDPOINTS:
#   GET  /     → 200 OK  (Render health check)
#   GET  /ws   → WebSocket upgrade (CyberApp clients connect here)
#
# UPDATE cyberapp.py relay URL to end with /ws:
#   wss://your-app.onrender.com/ws
#
# DEPENDENCIES:
#   pip install aiohttp
# =============================================================================

import asyncio
import json
import os
import time
import aiohttp
from aiohttp import web

# ── State ─────────────────────────────────────────────────────────────────────

clients: dict = {}
_id_counter = 0

def _new_id() -> str:
    global _id_counter
    _id_counter += 1
    return f"u{_id_counter}"

# ── Helpers ───────────────────────────────────────────────────────────────────

async def _broadcast_peer_list():
    peers = [
        {"id": cid, "nickname": info["nickname"]}
        for cid, info in clients.items()
    ]
    msg  = json.dumps({"type": "peer_list", "peers": peers})
    dead = []
    for cid, info in list(clients.items()):
        try:
            await info["ws"].send_str(msg)
        except Exception:
            dead.append(cid)
    for cid in dead:
        clients.pop(cid, None)

# ── WebSocket handler ─────────────────────────────────────────────────────────

async def websocket_handler(request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)

    conn_id  = _new_id()
    nickname = f"anon_{conn_id}"
    clients[conn_id] = {"ws": ws, "nickname": nickname, "joined": time.time()}
    print(f"[+] {conn_id} connected  (total: {len(clients)})")

    try:
        await ws.send_str(json.dumps({"type": "welcome", "your_id": conn_id}))
        await _broadcast_peer_list()

        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data  = json.loads(msg.data)
                    mtype = data.get("type")

                    if mtype == "announce":
                        nick = str(data.get("nickname", nickname))[:32].strip()
                        if nick:
                            clients[conn_id]["nickname"] = nick
                            print(f"    {conn_id} -> '{nick}'")
                            await _broadcast_peer_list()

                    elif mtype == "relay_text":
                        target_id = data.get("to")
                        if target_id and target_id in clients:
                            fwd = {
                                "type":      "relay_text",
                                "from":      conn_id,
                                "from_nick": clients[conn_id]["nickname"],
                                "payload":   data.get("payload"),
                            }
                            try:
                                await clients[target_id]["ws"].send_str(json.dumps(fwd))
                            except Exception:
                                pass

                    elif mtype == "ping":
                        await ws.send_str(json.dumps({"type": "pong"}))

                except (json.JSONDecodeError, KeyError):
                    pass

            elif msg.type == aiohttp.WSMsgType.BINARY:
                raw = msg.data
                if len(raw) < 5:
                    continue
                tid_len   = int.from_bytes(raw[:4], "big")
                if 4 + tid_len > len(raw):
                    continue
                target_id = raw[4:4 + tid_len].decode()
                blob      = raw[4 + tid_len:]
                if target_id in clients:
                    sid_bytes = conn_id.encode()
                    frame     = len(sid_bytes).to_bytes(4, "big") + sid_bytes + blob
                    try:
                        await clients[target_id]["ws"].send_bytes(frame)
                    except Exception:
                        pass

            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break

    finally:
        clients.pop(conn_id, None)
        print(f"[-] {conn_id} disconnected  (total: {len(clients)})")
        await _broadcast_peer_list()

    return ws

# ── Health check (satisfies Render HEAD/GET probes) ───────────────────────────

async def health_handler(request):
    return web.Response(
        text=f"CyberApp relay OK — {len(clients)} client(s) connected",
        status=200
    )

# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    port = int(os.environ.get("PORT", 8765))

    app = web.Application()
    app.router.add_get("/",   health_handler)    # Render health probe
    app.router.add_get("/ws", websocket_handler) # CyberApp clients

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    print(f"CyberApp Relay Server — port {port}")
    print(f"  Health : http://0.0.0.0:{port}/")
    print(f"  WS     : ws://0.0.0.0:{port}/ws")
    print("Waiting for clients...\n")

    await asyncio.Future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())
