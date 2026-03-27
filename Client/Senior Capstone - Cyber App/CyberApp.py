# Encrypted Messsaging App
# DEPENDENCIES:
# pip install cryptography Pillow websockets
# =============================================================================

import tkinter as tk
from tkinter import messagebox, scrolledtext, filedialog
from PIL import Image, ImageTk
import os, socket, threading, json, time, struct, base64, secrets, sys, subprocess
import asyncio
import queue
from pathlib import Path
from datetime import datetime

# ── Windows firewall helper ───────────────────────────────────────────────────
def _ensure_firewall_rule() -> bool:
    if sys.platform != "win32":
        return True
    rule_name = "CyberApp-P2P"
    try:
        check = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={rule_name}"],
            capture_output=True, text=True
        )
        if check.returncode == 0 and "No rules match" not in check.stdout:
            return True
        all_ok = True
        for protocol in ["TCP", "UDP"]:
            for port in [55555, 55556]:
                r = subprocess.run([
                    "netsh", "advfirewall", "firewall", "add", "rule",
                    f"name={rule_name}", "dir=in", "action=allow",
                    f"protocol={protocol}", f"localport={port}",
                ], capture_output=True, text=True)
                if r.returncode != 0:
                    all_ok = False
        return all_ok
    except Exception:
        return False

_FIREWALL_OK = _ensure_firewall_rule()

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ── Config ────────────────────────────────────────────────────────────────────

# 🔧 CHANGE THIS to your deployed server URL, e.g.:
#    "ws://your-app.railway.app"   or   "wss://your-app.onrender.com"
#    Leave as "" to disable relay (LAN-only mode)
RELAY_URL = ""

DARK_BG   = "#05053B"
DARK_MID  = "#0D0D5A"
ACCENT    = "#22B9FF"
TEAL      = "#2D5961"
TEXT_FG   = "#E0F7FF"
RELAY_COL = "#FFD700"   # gold — relay peers shown in this colour

FONT_MAIN = ("Consolas", 13)
FONT_BIG  = ("Consolas", 20, "bold")
FONT_SM   = ("Consolas", 11)

DISCOVERY_PORT     = 55555
CHAT_PORT          = 55556
BROADCAST_INTERVAL = 3

STORAGE_DIR = Path.home() / "Downloads" / "CyberApp" / "Received"
STORAGE_DIR.mkdir(parents=True, exist_ok=True)


# ── Crypto ────────────────────────────────────────────────────────────────────

def generate_keypair():
    priv      = X25519PrivateKey.generate()
    pub_bytes = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return priv, pub_bytes

def derive_session_key(private_key, peer_pub_bytes: bytes) -> bytes:
    peer_pub = X25519PublicKey.from_public_bytes(peer_pub_bytes)
    shared   = private_key.exchange(peer_pub)
    return HKDF(
        algorithm=hashes.SHA256(), length=32,
        salt=None, info=b"cyberapp-v3-session"
    ).derive(shared)

def encrypt(key: bytes, plaintext: bytes) -> bytes:
    nonce = secrets.token_bytes(12)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, None)

def decrypt(key: bytes, data: bytes) -> bytes:
    return AESGCM(key).decrypt(data[:12], data[12:], None)


# ── Helper ────────────────────────────────────────────────────────────────────

def center_window(window):
    window.update_idletasks()
    w, h = window.winfo_width(), window.winfo_height()
    sw, sh = window.winfo_screenwidth(), window.winfo_screenheight()
    window.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")


# ── LAN Discovery (unchanged) ─────────────────────────────────────────────────

class PeerDiscovery:
    def __init__(self, nickname, pub_bytes, on_peer_found, on_peer_lost, log=None):
        self.nickname      = nickname
        self.pub_bytes     = pub_bytes
        self.on_peer_found = on_peer_found
        self.on_peer_lost  = on_peer_lost
        self.peers         = {}
        self._stop         = threading.Event()
        self.local_ips, self.bcast_addrs = self._get_network_info()
        self._socks        = []
        self.log           = log

    def _get_network_info(self):
        import ipaddress
        local_ips   = {"127.0.0.1"}
        bcast_addrs = []
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ips.add(s.getsockname()[0])
            s.close()
        except Exception:
            pass
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                local_ips.add(info[4][0])
        except Exception:
            pass
        if sys.platform == "win32":
            try:
                result = subprocess.run(["ipconfig"], capture_output=True, text=True, timeout=3)
                current_ip = None
                for line in result.stdout.splitlines():
                    line = line.strip()
                    if "IPv4 Address" in line or "IP Address" in line:
                        parts = line.split(":", 1)
                        if len(parts) == 2:
                            current_ip = parts[1].strip().rstrip("(Preferred)").strip()
                            local_ips.add(current_ip)
                    elif "Subnet Mask" in line and current_ip:
                        parts = line.split(":", 1)
                        if len(parts) == 2:
                            import ipaddress as _ip
                            try:
                                net   = _ip.IPv4Network(f"{current_ip}/{parts[1].strip()}", strict=False)
                                bcast = str(net.broadcast_address)
                                if bcast not in ("255.255.255.255", "0.0.0.0"):
                                    bcast_addrs.append(bcast)
                            except Exception:
                                pass
                        current_ip = None
            except Exception:
                pass
        else:
            try:
                import re, ipaddress as _ip
                result = subprocess.run(["ip", "addr"], capture_output=True, text=True, timeout=3)
                for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", result.stdout):
                    ip, prefix = m.group(1), int(m.group(2))
                    local_ips.add(ip)
                    try:
                        net   = _ip.IPv4Network(f"{ip}/{prefix}", strict=False)
                        bcast = str(net.broadcast_address)
                        if bcast not in ("255.255.255.255", "0.0.0.0"):
                            bcast_addrs.append(bcast)
                    except Exception:
                        pass
            except Exception:
                pass
        if not bcast_addrs:
            bcast_addrs = ["<broadcast>"]
        return local_ips, list(dict.fromkeys(bcast_addrs))

    def start(self):
        threading.Thread(target=self._broadcast_loop, daemon=True).start()
        threading.Thread(target=self._listen_loop,    daemon=True).start()
        threading.Thread(target=self._prune_loop,     daemon=True).start()

    def _broadcast_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._socks.append(sock)
        payload = json.dumps({"type": "announce", "nickname": self.nickname, "port": CHAT_PORT}).encode()
        while not self._stop.is_set():
            for addr in self.bcast_addrs:
                try:
                    sock.sendto(payload, (addr, DISCOVERY_PORT))
                except Exception:
                    pass
            self._stop.wait(BROADCAST_INTERVAL)

    def _listen_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", DISCOVERY_PORT))
        except OSError as e:
            if self.log:
                self.log(f"[DISCOVERY] BIND FAILED: {e}")
            return
        sock.settimeout(1.0)
        self._socks.append(sock)
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(4096)
                msg        = json.loads(data.decode())
                peer_ip    = addr[0]
                if peer_ip in self.local_ips:
                    continue
                if msg.get("type") == "announce":
                    is_new = peer_ip not in self.peers
                    self.peers[peer_ip] = {
                        "nickname": msg["nickname"], "port": msg.get("port", CHAT_PORT),
                        "last_seen": time.time()
                    }
                    if is_new:
                        self.on_peer_found(peer_ip, msg["nickname"])
            except (socket.timeout, json.JSONDecodeError):
                pass
            except OSError:
                break

    def _prune_loop(self):
        while not self._stop.is_set():
            now  = time.time()
            dead = [ip for ip, d in self.peers.items() if now - d["last_seen"] > 10]
            for ip in dead:
                del self.peers[ip]
                self.on_peer_lost(ip)
            self._stop.wait(5)

    def stop(self):
        self._stop.set()
        for s in self._socks:
            try: s.close()
            except OSError: pass


class ChatServer:
    def __init__(self, on_incoming, log=None):
        self.on_incoming = on_incoming
        self._stop       = threading.Event()
        self._sock       = None
        self.log         = log

    def start(self):
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._sock.bind(("", CHAT_PORT))
        except OSError as e:
            if self.log:
                self.log(f"[SERVER] BIND FAILED: {e}")
            return
        self._sock.listen(5)
        self._sock.settimeout(1.0)
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
                threading.Thread(target=self.on_incoming, args=(conn, addr[0]), daemon=True).start()
            except socket.timeout:
                pass
            except OSError:
                break

    def stop(self):
        self._stop.set()
        if self._sock:
            try: self._sock.close()
            except OSError: pass


# ── LAN Encrypted Session (unchanged) ────────────────────────────────────────

class EncryptedSession:
    """Direct TCP session — used for LAN peers."""
    MAGIC = b"CYBERAPP_V3"

    def __init__(self, sock: socket.socket, is_initiator: bool):
        self.sock  = sock
        self.key   = None
        self._lock = threading.Lock()

        priv, my_pub = generate_keypair()
        if is_initiator:
            self._send_raw(self.MAGIC + my_pub)
            their_data = self._recv_raw()
        else:
            their_data = self._recv_raw()
            self._send_raw(self.MAGIC + my_pub)

        assert their_data[:len(self.MAGIC)] == self.MAGIC, "Handshake failed: bad magic"
        self.key = derive_session_key(priv, their_data[len(self.MAGIC):])

    def send_text(self, payload: dict):
        enc = encrypt(self.key, json.dumps(payload).encode())
        with self._lock:
            self._send_raw(enc)

    def send_file(self, msg_type: str, raw: bytes, filename: str):
        header     = json.dumps({"type": msg_type, "filename": filename, "size": len(raw)}).encode()
        combined   = struct.pack(">I", len(header)) + header + raw
        with self._lock:
            self._send_raw(encrypt(self.key, combined))

    def recv(self):
        data = decrypt(self.key, self._recv_raw())
        try:
            hlen = struct.unpack(">I", data[:4])[0]
            if 0 < hlen < 4096 and 4 + hlen <= len(data):
                header = json.loads(data[4:4 + hlen].decode())
                return header.get("type"), header, data[4 + hlen:]
        except Exception:
            pass
        msg = json.loads(data.decode())
        return msg.get("type"), msg, None

    def close(self):
        try: self.sock.shutdown(socket.SHUT_RDWR)
        except OSError: pass
        try: self.sock.close()
        except OSError: pass

    def _send_raw(self, data: bytes):
        self.sock.sendall(struct.pack(">I", len(data)) + data)

    def _recv_raw(self) -> bytes:
        length = struct.unpack(">I", self._recvn(4))[0]
        return self._recvn(length)

    def _recvn(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionResetError("Peer disconnected")
            buf += chunk
        return buf


# ── Relay Session ─────────────────────────────────────────────────────────────

class RelaySession:
    """
    Virtual encrypted session that tunnels through the relay server.
    The wire format is the same as EncryptedSession but carried over WebSocket
    binary frames instead of a direct TCP socket.

    The relay_client (RelayClient) owns the single WebSocket connection.
    RelaySession talks to it via thread-safe queues.
    """

    def __init__(self, relay_client, peer_id: str, is_initiator: bool):
        self._relay      = relay_client
        self._peer_id    = peer_id
        self._lock       = threading.Lock()
        self._recv_queue = queue.Queue()  # bytes pushed here by RelayClient
        self.key         = None

        # Register ourselves so RelayClient routes inbound blobs here
        relay_client.register_session(peer_id, self)

        # ── Handshake ──
        priv, my_pub = generate_keypair()
        MAGIC = b"CYBERAPP_V3"

        if is_initiator:
            self._send_raw(MAGIC + my_pub)
            their_data = self._recv_raw()
        else:
            their_data = self._recv_raw()
            self._send_raw(MAGIC + my_pub)

        assert their_data[:len(MAGIC)] == MAGIC, "Relay handshake failed"
        self.key = derive_session_key(priv, their_data[len(MAGIC):])

    # ── Public interface (matches EncryptedSession) ──

    def send_text(self, payload: dict):
        enc = encrypt(self.key, json.dumps(payload).encode())
        with self._lock:
            self._send_raw(enc)

    def send_file(self, msg_type: str, raw: bytes, filename: str):
        header   = json.dumps({"type": msg_type, "filename": filename, "size": len(raw)}).encode()
        combined = struct.pack(">I", len(header)) + header + raw
        with self._lock:
            self._send_raw(encrypt(self.key, combined))

    def recv(self):
        while True:
            data = decrypt(self.key, self._recv_raw())
            try:
                hlen = struct.unpack(">I", data[:4])[0]
                if 0 < hlen < 4096 and 4 + hlen <= len(data):
                    header = json.loads(data[4:4 + hlen].decode())
                    return header.get("type"), header, data[4 + hlen:]
            except Exception:
                pass
            msg = json.loads(data.decode())
            return msg.get("type"), msg, None

    def close(self):
        self._relay.unregister_session(self._peer_id)

    # ── Internal ──

    def _send_raw(self, data: bytes):
        """Wrap data with target peer ID and send via relay."""
        pid_bytes = self._peer_id.encode()
        frame     = len(pid_bytes).to_bytes(4, "big") + pid_bytes + data
        self._relay.send_binary(frame)

    def _recv_raw(self) -> bytes:
        """Block until a blob arrives from the relay for this session."""
        while True:
            try:
                return self._recv_queue.get(timeout=1.0)
            except queue.Empty:
                if self._relay.closed:
                    raise ConnectionResetError("Relay disconnected")

    def push(self, blob: bytes):
        """Called by RelayClient when a binary frame arrives from our peer."""
        self._recv_queue.put(blob)


# ── Relay Client ──────────────────────────────────────────────────────────────

class RelayClient:
    """
    Manages a single persistent WebSocket connection to the relay server.
    Handles peer list updates and routes binary blobs to the right RelaySession.
    """

    def __init__(self, url: str, nickname: str,
                 on_peer_found, on_peer_lost, on_incoming, log=None):
        self._url           = url
        self._nickname      = nickname
        self._on_peer_found = on_peer_found
        self._on_peer_lost  = on_peer_lost
        self._on_incoming   = on_incoming
        self._log           = log

        self.my_id          = None
        self.closed         = False
        self._ws            = None
        self._sessions      = {}   # peer_id → RelaySession
        self._known_peers   = set()
        self._loop          = None
        self._send_queue    = asyncio.Queue() if False else None  # set up in thread
        self._thread        = None

    def start(self):
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._send_queue = asyncio.Queue()
        try:
            self._loop.run_until_complete(self._connect())
        except Exception as e:
            if self._log:
                self._log(f"[RELAY] Fatal error: {e}")

    async def _connect(self):
        import websockets
        retry_delay = 2
        while not self.closed:
            try:
                if self._log:
                    self._log(f"[RELAY] Connecting to {self._url} …")
                async with websockets.connect(self._url, ping_interval=20) as ws:
                    self._ws   = ws
                    retry_delay = 2
                    if self._log:
                        self._log(f"[RELAY] Connected!")
                    await asyncio.gather(
                        self._recv_loop(ws),
                        self._send_loop(ws),
                    )
            except Exception as e:
                if self.closed:
                    break
                if self._log:
                    self._log(f"[RELAY] Disconnected ({e}), retrying in {retry_delay}s …")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30)

    async def _recv_loop(self, ws):
        async for raw in ws:
            if isinstance(raw, str):
                try:
                    msg   = json.loads(raw)
                    mtype = msg.get("type")

                    if mtype == "welcome":
                        self.my_id = msg["your_id"]
                        # Announce our nickname
                        await ws.send(json.dumps({"type": "announce", "nickname": self._nickname}))
                        if self._log:
                            self._log(f"[RELAY] My relay ID: {self.my_id}")

                    elif mtype == "peer_list":
                        peers = {p["id"]: p["nickname"] for p in msg.get("peers", [])}
                        # Remove ourselves
                        peers.pop(self.my_id, None)

                        new_ids  = set(peers) - self._known_peers
                        gone_ids = self._known_peers - set(peers)

                        for pid in new_ids:
                            self._known_peers.add(pid)
                            relay_id = f"relay:{pid}"
                            if self._log:
                                self._log(f"[RELAY] New peer: {peers[pid]} ({pid})")
                            self._on_peer_found(relay_id, peers[pid])

                        for pid in gone_ids:
                            self._known_peers.discard(pid)
                            relay_id = f"relay:{pid}"
                            self._on_peer_lost(relay_id)

                    elif mtype == "pong":
                        pass

                except (json.JSONDecodeError, KeyError):
                    pass

            elif isinstance(raw, bytes):
                # Format: 4-byte sender_id_len + sender_id + blob
                if len(raw) < 5:
                    continue
                sid_len   = int.from_bytes(raw[:4], "big")
                sender_id = raw[4:4 + sid_len].decode()
                blob      = raw[4 + sid_len:]
                relay_id  = f"relay:{sender_id}"

                if sender_id in self._sessions:
                    self._sessions[sender_id].push(blob)
                else:
                    # Incoming new session — someone is initiating a chat with us
                    threading.Thread(
                        target=self._accept_incoming,
                        args=(sender_id, blob),
                        daemon=True
                    ).start()

    async def _send_loop(self, ws):
        while True:
            item = await self._send_queue.get()
            if item is None:
                break
            try:
                await ws.send(item)
            except Exception:
                break

    def send_binary(self, frame: bytes):
        """Thread-safe: schedule a binary send on the relay WebSocket."""
        if self._loop and not self.closed:
            asyncio.run_coroutine_threadsafe(
                self._send_queue.put(frame), self._loop
            )

    def _accept_incoming(self, sender_id: str, first_blob: bytes):
        """
        Called when the first binary blob arrives from a peer we have no session with.
        We create a RelaySession for the responder side, pre-filling its queue with
        the first blob so the handshake can complete.
        """
        session = RelaySession.__new__(RelaySession)
        session._relay      = self
        session._peer_id    = sender_id
        session._lock       = threading.Lock()
        session._recv_queue = queue.Queue()
        session.key         = None
        self._sessions[sender_id] = session
        session._recv_queue.put(first_blob)

        # Complete handshake as responder
        try:
            priv, my_pub = generate_keypair()
            MAGIC = b"CYBERAPP_V3"
            their_data = session._recv_raw()
            session._send_raw(MAGIC + my_pub)
            assert their_data[:len(MAGIC)] == MAGIC
            session.key = derive_session_key(priv, their_data[len(MAGIC):])
            self._on_incoming(session, f"relay:{sender_id}")
        except Exception as e:
            if self._log:
                self._log(f"[RELAY] Incoming handshake failed from {sender_id}: {e}")
            self._sessions.pop(sender_id, None)

    def register_session(self, peer_id: str, session: "RelaySession"):
        # peer_id here is the raw relay ID (without "relay:" prefix)
        self._sessions[peer_id] = session

    def unregister_session(self, peer_id: str):
        self._sessions.pop(peer_id, None)

    def stop(self):
        self.closed = True
        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self._send_queue.put(None), self._loop
            )


# ── Messaging Window (unchanged from original) ────────────────────────────────

class MessagingWindow:
    def __init__(self, parent, session, peer_nickname: str, my_nickname: str,
                 is_relay: bool = False):
        self.parent        = parent
        self.session       = session
        self.peer_nickname = peer_nickname
        self.my_nickname   = my_nickname
        self._closed       = False
        self._photos       = []
        self._is_relay     = is_relay

        self.win = tk.Toplevel(parent)
        route    = "🌐 Internet" if is_relay else "🔒 LAN"
        self.win.title(f"{route} Chat with {peer_nickname}")
        self.win.configure(bg=DARK_BG)
        self.win.geometry("700x760")
        self.win.protocol("WM_DELETE_WINDOW", self._on_close)

        tk.Label(self.win,
                 text=f"{'🌐' if is_relay else '🔒'}  Encrypted Session  ▸  {peer_nickname}",
                 font=FONT_BIG, bg=DARK_BG, fg=RELAY_COL if is_relay else ACCENT).pack(pady=(14, 2))
        route_tag = "via relay server  •  " if is_relay else ""
        tk.Label(self.win,
                 text=f"{route_tag}",
                 font=FONT_SM, bg=DARK_BG, fg="#3A8899").pack()
        tk.Frame(self.win, height=1, bg=TEAL).pack(fill="x", pady=8, padx=20)

        self.msg_area = scrolledtext.ScrolledText(
            self.win, font=FONT_MAIN, bg=DARK_MID, fg=TEXT_FG,
            insertbackground=ACCENT, relief="flat",
            state="disabled", wrap="word", padx=10, pady=10
        )
        self.msg_area.pack(fill="both", expand=True, padx=20, pady=(0, 8))
        self.msg_area.tag_config("me",    foreground=ACCENT)
        self.msg_area.tag_config("peer",  foreground="#7DFFB0")
        self.msg_area.tag_config("sys",   foreground="#888888",
                                           font=("Consolas", 10, "italic"))
        self.msg_area.tag_config("err",   foreground="#FF6B6B")

        input_frame = tk.Frame(self.win, bg=DARK_BG)
        input_frame.pack(fill="x", padx=20, pady=(0, 14))

        self.entry = tk.Entry(input_frame, font=FONT_MAIN,
                              bg=DARK_MID, fg=TEXT_FG,
                              insertbackground=ACCENT, relief="flat")
        self.entry.pack(side="left", fill="x", expand=True, ipady=8, padx=(0, 8))
        self.entry.bind("<Return>", lambda e: self._send_text())

        tk.Button(input_frame, text="Send", font=FONT_MAIN,
                  bg=TEAL, fg=RELAY_COL if is_relay else ACCENT,
                  relief="flat", cursor="hand2",
                  command=self._send_text).pack(side="left", ipady=8, ipadx=12)

        tk.Button(input_frame, text="📎", font=("Consolas", 16),
                  bg=TEAL, fg=ACCENT, relief="flat", cursor="hand2",
                  command=self._send_file).pack(side="left", ipady=4, ipadx=8, padx=(6, 0))

        center_window(self.win)
        conn_type = "relay server (internet)" if is_relay else "LAN"
        self._append("sys",
            f"Session established with {peer_nickname} via {conn_type}.\n"
            f"All messages and files are end-to-end encrypted.\n"
            f"The relay server cannot read any of your messages.\n"
            f"Received files saved to: {STORAGE_DIR}\n")

        threading.Thread(target=self._recv_loop, daemon=True).start()

    def _append(self, tag, text):
        def _do():
            ts = datetime.now().strftime("%H:%M")
            self.msg_area.configure(state="normal")
            self.msg_area.insert("end", f"[{ts}] {text}\n", tag)
            self.msg_area.configure(state="disabled")
            self.msg_area.see("end")
        if self.win.winfo_exists():
            self.win.after(0, _do)

    def _send_text(self):
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, "end")
        try:
            self.session.send_text({"type": "text", "text": text, "nick": self.my_nickname})
            self._append("me", f"You: {text}")
        except Exception as e:
            self._append("err", f"Send error: {e}")

    def _send_file(self):
        path = filedialog.askopenfilename(title="Select file to send")
        if not path:
            return
        path  = Path(path)
        raw   = path.read_bytes()
        ext   = path.suffix.lower()
        ftype = "image" if ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp") else "file"
        try:
            self.session.send_file(ftype, raw, path.name)
            self._append("me", f"You sent {ftype}: {path.name} ({len(raw):,} bytes)")
        except Exception as e:
            self._append("err", f"File send error: {e}")

    def _recv_loop(self):
        while not self._closed:
            try:
                msg_type, meta, raw = self.session.recv()
                if msg_type == "text":
                    nick = meta.get("nick", self.peer_nickname)
                    self._append("peer", f"{nick}: {meta.get('text', '')}")
                elif msg_type in ("image", "file"):
                    filename  = meta.get("filename", f"recv_{int(time.time())}")
                    save_path = STORAGE_DIR / filename
                    stem, suffix = Path(filename).stem, Path(filename).suffix
                    counter = 1
                    while save_path.exists():
                        save_path = STORAGE_DIR / f"{stem}_{counter}{suffix}"
                        counter  += 1
                    save_path.write_bytes(raw)
                    self._append("peer",
                        f"{self.peer_nickname} sent {msg_type}: "
                        f"{filename} ({len(raw):,} bytes)  →  {save_path}")
                    if msg_type == "image":
                        self._show_preview(save_path)
                elif msg_type == "bye":
                    self._append("sys", f"{self.peer_nickname} ended the session.")
                    break
            except ConnectionResetError:
                self._append("sys", "Connection closed by peer.")
                break
            except Exception as e:
                if not self._closed:
                    self._append("err", f"Receive error: {e}")
                break

    def _show_preview(self, path: Path):
        def _do():
            try:
                img   = Image.open(path)
                img.thumbnail((280, 280))
                photo = ImageTk.PhotoImage(img)
                self.msg_area.configure(state="normal")
                self.msg_area.image_create("end", image=photo)
                self.msg_area.insert("end", "\n")
                self.msg_area.configure(state="disabled")
                self.msg_area.see("end")
                self._photos.append(photo)
            except Exception:
                pass
        if self.win.winfo_exists():
            self.win.after(0, _do)

    def _on_close(self):
        self._closed = True
        try:
            self.session.send_text({"type": "bye"})
        except Exception:
            pass
        self.session.close()
        self.win.destroy()


# ── Chat Window (lobby) ───────────────────────────────────────────────────────

class ChatWindow:
    def __init__(self, parent, nickname):
        self.parent        = parent
        self.nickname      = nickname
        self.sessions      = {}
        self._peer_map     = {}   # peer_key → {"nickname", "type": "lan"|"relay", ...}
        self._relay_client = None

        self.win = tk.Toplevel(parent)
        self.win.title("CyberApp — Lobby")
        self.win.configure(bg=DARK_BG)
        self.win.geometry("1000x720")
        self.win.protocol("WM_DELETE_WINDOW", self._on_close)

        # ── Header ──
        tk.Label(self.win, text="CYBERAPP",
                 font=("Consolas", 28, "bold"), bg=DARK_BG, fg=ACCENT).pack(pady=(20, 0))
        tk.Label(self.win, text=f"Logged in as  ›  {nickname}",
                 font=FONT_SM, bg=DARK_BG, fg=TEAL).pack()

        # Relay status badge
        self._relay_var = tk.StringVar(value="⬤  Relay: disabled" if not RELAY_URL else "⬤  Relay: connecting…")
        relay_color     = "#444466" if not RELAY_URL else "#FFD700"
        self._relay_lbl = tk.Label(self.win, textvariable=self._relay_var,
                                    font=FONT_SM, bg=DARK_BG, fg=relay_color)
        self._relay_lbl.pack()

        # Relay URL entry
        relay_frame = tk.Frame(self.win, bg=DARK_BG)
        relay_frame.pack(fill="x", padx=30, pady=(4, 0))
        tk.Label(relay_frame, text="Relay URL:", font=FONT_SM,
                 bg=DARK_BG, fg=TEAL).pack(side="left")
        self._relay_entry = tk.Entry(relay_frame, font=FONT_SM,
                                      bg=DARK_MID, fg=TEXT_FG,
                                      insertbackground=ACCENT, relief="flat", width=44)
        self._relay_entry.pack(side="left", padx=8, ipady=4)
        self._relay_entry.insert(0, RELAY_URL or "ws://yourserver:8765")
        tk.Button(relay_frame, text="Connect", font=FONT_SM,
                  bg=TEAL, fg=ACCENT, relief="flat", cursor="hand2",
                  command=self._connect_relay).pack(side="left", ipadx=8, ipady=4)

        tk.Frame(self.win, height=1, bg=TEAL).pack(fill="x", padx=30, pady=10)

        # ── Main columns ──
        cols = tk.Frame(self.win, bg=DARK_BG)
        cols.pack(fill="both", expand=True, padx=30)

        left = tk.Frame(cols, bg=DARK_MID)
        left.pack(side="left", fill="y", padx=(0, 12), ipadx=10, ipady=10)

        tk.Label(left, text="PEERS ONLINE",
                 font=("Consolas", 11, "bold"), bg=DARK_MID, fg=TEAL).pack(pady=(10, 4))

        self.peer_listbox = tk.Listbox(
            left, font=FONT_MAIN, bg=DARK_MID, fg=ACCENT,
            selectbackground=TEAL, selectforeground=TEXT_FG,
            relief="flat", borderwidth=0, width=26, height=20, activestyle="none"
        )
        self.peer_listbox.pack(padx=8, pady=4, fill="y", expand=True)
        self.peer_listbox.bind("<<ListboxSelect>>", self._on_peer_select)
        self.peer_listbox.bind("<Double-Button-1>",  self._on_peer_double_click)

        tk.Label(left, text="🟡 = internet  🔵 = LAN",
                 font=("Consolas", 9), bg=DARK_MID, fg="#666688").pack(pady=(4, 2))

        tk.Button(left, text="⟳  Refresh", font=FONT_SM,
                  bg=TEAL, fg=ACCENT, relief="flat", cursor="hand2",
                  command=self._manual_refresh).pack(pady=8, padx=8, fill="x")

        right = tk.Frame(cols, bg=DARK_BG)
        right.pack(side="left", fill="both", expand=True)

        self.info_label = tk.Label(right,
            text="Scanning for peers on your network…\n\n"
                 "Enter a relay URL above to also find\npeers over the internet.\n\n"
                 "Double-click a peer to open an\nencrypted session.",
            font=("Consolas", 14), bg=DARK_BG, fg="#3A8899", justify="left")
        self.info_label.pack(anchor="nw", pady=20)

        self.connect_btn = tk.Button(right, text="🔒  Start Encrypted Chat",
            font=FONT_MAIN, bg=TEAL, fg=ACCENT, relief="flat", cursor="hand2",
            state="disabled", command=self._start_chat_with_selected)
        self.connect_btn.pack(anchor="nw", pady=8, ipadx=12, ipady=8)

        self.status_var = tk.StringVar(value="Scanning network…")
        tk.Label(self.win, textvariable=self.status_var,
                 font=FONT_SM, bg=DARK_BG, fg="#3A8899",
                 anchor="w").pack(side="bottom", fill="x", padx=30, pady=8)

        # ── Debug console ──
        self._debug_visible = False
        self._debug_frame   = tk.Frame(self.win, bg="#03030F")
        self._debug_area    = scrolledtext.ScrolledText(
            self._debug_frame, font=("Consolas", 10),
            bg="#03030F", fg="#00FF88", height=8, state="disabled", relief="flat"
        )
        self._debug_area.pack(fill="both", expand=True, padx=8, pady=4)
        tk.Button(self.win, text="[ debug ]", font=("Consolas", 9),
                  bg=DARK_BG, fg=TEAL, relief="flat", cursor="hand2",
                  command=self._toggle_debug).pack(side="bottom", anchor="e", padx=30)

        center_window(self.win)
        self.win.focus_force()

        _, self._pub_bytes = generate_keypair()

        # Start LAN discovery
        self._discovery = PeerDiscovery(
            nickname=nickname, pub_bytes=self._pub_bytes,
            on_peer_found=self._on_peer_found_lan,
            on_peer_lost=self._on_peer_lost,
            log=self._debug_log
        )
        self._discovery.start()

        self._server = ChatServer(
            on_incoming=self._on_incoming_connection_lan,
            log=self._debug_log
        )
        self._server.start()

        # Auto-connect relay if URL is configured
        if RELAY_URL:
            self.win.after(500, self._connect_relay)

    # ── Relay ──

    def _connect_relay(self):
        url = self._relay_entry.get().strip()
        if not url or url == "ws://yourserver:8765":
            messagebox.showinfo("Relay", "Enter your relay server URL first.\n\n"
                                         "Example: ws://192.168.1.10:8765\n"
                                         "Or:      wss://myapp.railway.app")
            return
        if self._relay_client:
            self._relay_client.stop()
            self._relay_client = None

        self._relay_var.set("⬤  Relay: connecting…")
        self._relay_lbl.config(fg="#FFD700")

        self._relay_client = RelayClient(
            url          = url,
            nickname     = self.nickname,
            on_peer_found= self._on_peer_found_relay,
            on_peer_lost = self._on_peer_lost,
            on_incoming  = self._on_incoming_connection_relay,
            log          = self._debug_log
        )
        self._relay_client.start()

        # Poll until we get our ID (= connected)
        self.win.after(500, self._check_relay_connected)

    def _check_relay_connected(self):
        if self._relay_client and self._relay_client.my_id:
            self._relay_var.set(f"⬤  Relay: connected  (id: {self._relay_client.my_id})")
            self._relay_lbl.config(fg="#00FF88")
        elif self._relay_client and not self._relay_client.closed:
            self.win.after(500, self._check_relay_connected)

    # ── Peer events ──

    def _on_peer_found_lan(self, ip, peer_nickname):
        key = ip
        self._peer_map[key] = {
            "nickname": peer_nickname, "type": "lan",
            "ip": ip, "port": self._discovery.peers.get(ip, {}).get("port", CHAT_PORT)
        }
        self.win.after(0, self._rebuild_listbox)
        self.win.after(0, lambda: self.status_var.set(f"LAN peer: {peer_nickname} ({ip})"))

    def _on_peer_found_relay(self, relay_key, peer_nickname):
        self._peer_map[relay_key] = {
            "nickname": peer_nickname, "type": "relay",
            "relay_id": relay_key.replace("relay:", "")
        }
        self.win.after(0, self._rebuild_listbox)
        self.win.after(0, lambda: self.status_var.set(f"Internet peer: {peer_nickname}"))

    def _on_peer_lost(self, key):
        self._peer_map.pop(key, None)
        self.win.after(0, self._rebuild_listbox)

    def _rebuild_listbox(self):
        self.peer_listbox.delete(0, "end")
        for key, info in self._peer_map.items():
            icon = "🟡" if info["type"] == "relay" else "🔵"
            self.peer_listbox.insert("end", f"{icon} {info['nickname']}  ({key})")

    def _manual_refresh(self):
        self._rebuild_listbox()
        self.status_var.set(f"{len(self._peer_map)} peer(s) visible.")

    # ── Selection & connect ──

    def _selected_peer_key(self):
        sel = self.peer_listbox.curselection()
        if not sel:
            return None
        label = self.peer_listbox.get(sel[0])
        # Extract key between last "(" and ")"
        try:
            return label.split("(")[-1].rstrip(")").strip()
        except Exception:
            return None

    def _on_peer_select(self, event):
        key = self._selected_peer_key()
        if key and key in self._peer_map:
            info  = self._peer_map[key]
            badge = "🌐 Internet" if info["type"] == "relay" else "🔒 LAN"
            self.info_label.config(
                text=f"Peer:  {info['nickname']}\nID:    {key}"
                     f"\nRoute: {badge}\n\nDouble-click to open encrypted session.")
            self.connect_btn.config(state="normal",
                fg=RELAY_COL if info["type"] == "relay" else ACCENT)

    def _on_peer_double_click(self, event):
        self._start_chat_with_selected()

    def _start_chat_with_selected(self):
        key = self._selected_peer_key()
        if not key or key not in self._peer_map:
            return
        if key in self.sessions:
            try: self.sessions[key].win.lift()
            except: pass
            return
        info = self._peer_map[key]
        if info["type"] == "lan":
            threading.Thread(
                target=self._connect_thread_lan,
                args=(key, info["ip"], info.get("port", CHAT_PORT), info["nickname"]),
                daemon=True
            ).start()
        else:
            threading.Thread(
                target=self._connect_thread_relay,
                args=(key, info["relay_id"], info["nickname"]),
                daemon=True
            ).start()

    # ── LAN connect ──

    def _connect_thread_lan(self, key, ip, port, peer_nickname):
        self._debug_log(f"[CONNECT/LAN] TCP to {ip}:{port}")
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(8)
            sock.connect((ip, port))
            sock.settimeout(None)
            session = EncryptedSession(sock, is_initiator=True)
            self.win.after(0, lambda: self._open_messaging(
                session, key, peer_nickname, is_relay=False))
        except Exception as e:
            self._debug_log(f"[CONNECT/LAN] FAILED: {e}")
            self.win.after(0, lambda err=e: messagebox.showerror(
                "Connection Failed", f"Could not connect to {peer_nickname}:\n{err}"))

    def _on_incoming_connection_lan(self, conn, peer_ip):
        try:
            session       = EncryptedSession(conn, is_initiator=False)
            info          = self._peer_map.get(peer_ip, {})
            peer_nickname = info.get("nickname", peer_ip)
            self.win.after(0, lambda: self._open_messaging(
                session, peer_ip, peer_nickname, is_relay=False))
        except Exception as e:
            self._debug_log(f"[SERVER] Handshake failed from {peer_ip}: {e}")

    # ── Relay connect ──

    def _connect_thread_relay(self, key, relay_id, peer_nickname):
        if not self._relay_client:
            self.win.after(0, lambda: messagebox.showerror(
                "Relay", "Not connected to a relay server.\n"
                          "Enter a relay URL and click Connect first."))
            return
        self._debug_log(f"[CONNECT/RELAY] Initiating to relay ID {relay_id}")
        try:
            session = RelaySession(self._relay_client, relay_id, is_initiator=True)
            self.win.after(0, lambda: self._open_messaging(
                session, key, peer_nickname, is_relay=True))
        except Exception as e:
            self._debug_log(f"[CONNECT/RELAY] FAILED: {e}")
            self.win.after(0, lambda err=e: messagebox.showerror(
                "Connection Failed", f"Could not connect to {peer_nickname} via relay:\n{err}"))

    def _on_incoming_connection_relay(self, session, relay_key):
        relay_id = relay_key.replace("relay:", "")
        info     = self._peer_map.get(relay_key, {})
        nickname = info.get("nickname", relay_id)
        self.win.after(0, lambda: self._open_messaging(
            session, relay_key, nickname, is_relay=True))

    # ── Open window ──

    def _open_messaging(self, session, key, peer_nickname, is_relay: bool):
        if key in self.sessions:
            try: self.sessions[key].win.lift()
            except: pass
            return
        mw = MessagingWindow(self.win, session, peer_nickname,
                              self.nickname, is_relay=is_relay)
        self.sessions[key] = mw

    # ── Debug ──

    def _debug_log(self, msg: str):
        def _do():
            ts = datetime.now().strftime("%H:%M:%S")
            self._debug_area.configure(state="normal")
            self._debug_area.insert("end", f"[{ts}] {msg}\n")
            self._debug_area.configure(state="disabled")
            self._debug_area.see("end")
        try:
            self.win.after(0, _do)
        except Exception:
            pass

    def _toggle_debug(self):
        if self._debug_visible:
            self._debug_frame.pack_forget()
            self._debug_visible = False
        else:
            self._debug_frame.pack(side="bottom", fill="x", padx=10, pady=(0, 4))
            self._debug_visible = True

    def _on_close(self):
        self._discovery.stop()
        self._server.stop()
        if self._relay_client:
            self._relay_client.stop()
        for mw in list(self.sessions.values()):
            try: mw._on_close()
            except: pass
        self.parent.deiconify()
        self.win.destroy()


# ── App shell ─────────────────────────────────────────────────────────────────

class CyberApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("CyberApp")
        self.root.configure(bg=DARK_BG)
        self.root.geometry("800x600")

        tk.Label(self.root, text="CYBERAPP",
                 font=("Consolas", 36, "bold"), bg=DARK_BG, fg=ACCENT).pack(pady=(60, 4))
        tk.Label(self.root,
                 text="end-to-end encrypted  •  peer-to-peer  •  ephemeral",
                 font=FONT_SM, bg=DARK_BG, fg=TEAL).pack()
        tk.Label(self.root,
                 text="LAN  +  internet relay",
                 font=FONT_SM, bg=DARK_BG, fg=RELAY_COL).pack(pady=(2, 0))
        tk.Button(self.root, text="Start Chatting  →",
                  font=("Consolas", 18), bg=TEAL, fg=ACCENT,
                  relief="flat", cursor="hand2",
                  command=self._on_start).pack(pady=40, ipadx=20, ipady=10)

        if os.path.exists("cyberlogo.png"):
            try:
                logo  = Image.open("cyberlogo.png").resize((220, 220))
                photo = ImageTk.PhotoImage(logo)
                lbl   = tk.Label(self.root, image=photo, bg=DARK_BG)
                lbl.image = photo
                lbl.pack()
            except Exception:
                pass

        center_window(self.root)
        if not _FIREWALL_OK and sys.platform == "win32":
            self.root.after(500, self._warn_firewall)

    def _warn_firewall(self):
        messagebox.showwarning(
            "Firewall — Action Required",
            "CyberApp couldn't automatically add a Windows Firewall rule.\n\n"
            "Other LAN PCs may not be able to connect to you.\n"
            "(Internet relay connections are unaffected.)\n\n"
            "Fix: Right-click cyberapp.py → 'Run as administrator'\n"
            "OR manually allow ports 55555-55556 (TCP+UDP) in Windows Firewall."
        )

    def _on_start(self):
        self.root.withdraw()
        SignupWindow(self.root)

    def run(self):
        self.root.mainloop()


class SignupWindow:
    def __init__(self, parent):
        self.parent = parent
        self.win    = tk.Toplevel(parent)
        self.win.title("Profile Setup")
        self.win.configure(bg=DARK_BG)
        self.win.geometry("420x320")
        self.win.protocol("WM_DELETE_WINDOW", self._on_close)

        tk.Label(self.win, text="Choose Your Nickname",
                 font=FONT_BIG, bg=DARK_BG, fg=ACCENT).pack(pady=(40, 6))
        tk.Label(self.win,
                 text="This is how others will see you on the network.",
                 font=FONT_SM, bg=DARK_BG, fg=TEAL).pack()

        self.entry = tk.Entry(self.win, font=("Consolas", 18),
                              bg=DARK_MID, fg=TEXT_FG,
                              insertbackground=ACCENT, relief="flat", justify="center")
        self.entry.pack(pady=20, ipadx=10, ipady=8, padx=40, fill="x")
        self.entry.bind("<Return>", lambda e: self._submit())

        tk.Button(self.win, text="Enter  →", font=("Consolas", 16),
                  bg=TEAL, fg=ACCENT, relief="flat", cursor="hand2",
                  command=self._submit).pack(pady=10, ipadx=16, ipady=8)

        center_window(self.win)
        self.entry.focus_set()

    def _submit(self):
        nickname = self.entry.get().strip()
        if not nickname:
            messagebox.showerror("Error", "Please enter a nickname.")
            return
        self.win.destroy()
        ChatWindow(self.parent, nickname)

    def _on_close(self):
        self.parent.deiconify()
        self.win.destroy()


if __name__ == "__main__":
    CyberApp().run()