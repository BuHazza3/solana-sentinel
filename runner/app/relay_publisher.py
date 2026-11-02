#!/usr/bin/env python3
"""Pushes the app's live state (/api/state on 127.0.0.1:APP_PORT) to a public relay so the GitHub Pages app can show it,
and receives the app's authenticated on/off commands for the bots' sub-agents (toggle_cmd.py).
The box only allows outbound HTTPS/443 and no inbound connections, so data is PUSHED out:
  primary : MQTT over WebSocket/TLS 443 to public.cloud.shiftr.io (retained message = latest state, live push)
  fallback: ntfy.sh attachment every NTFY_EVERY s (anonymous ntfy allows only ~250 messages/day per IP)
Every state payload is gzip + AES-256-GCM encrypted with a key derived from the secret in .relay_topic, so the public relay
and anyone who sees the topic name can't read it or forge it. Commands use a separate derived topic and key (toggle_cmd.py).
The ONLY thing this process writes for the bots is <BOT_DIR>/data/agent_toggles.json (sub-agent switches; paper only)."""
import gzip, hashlib, json, os, secrets, sys, time
import requests
import paho.mqtt.client as mqtt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import toggle_cmd

APP = os.path.dirname(os.path.abspath(__file__))
SECRET = open(os.path.join(APP, ".relay_topic")).read().strip()
TOKEN = open(os.path.join(APP, ".token")).read().strip()
H = lambda s: hashlib.sha256(s.encode()).digest()
TOPIC = "sntl/" + H("sentinel-topic:" + SECRET).hex()[:32]
NTFY_TOPIC = "sntl_" + H("sentinel-ntfy:" + SECRET).hex()[:32]
AES = AESGCM(H("sentinel-key:" + SECRET))
CMD_TOPIC, CMD_AES = toggle_cmd.derive(SECRET)
BOT_DIR = os.environ.get("BOT_DIR", "/workspace/solana-bot")
BROKER, PORT, WSPATH = "public.cloud.shiftr.io", 443, "/"
MIN_GAP, HEARTBEAT, NTFY_EVERY = 2.0, 10.0, int(os.environ.get("NTFY_EVERY", "900"))
STATUS = os.path.join(APP, "logs", "relay_status.json")
APP_PORT = int(os.environ.get("APP_PORT", "8787"))
os.makedirs(os.path.dirname(STATUS), exist_ok=True)
HANDLER = toggle_cmd.Handler(CMD_AES, BOT_DIR, os.path.join(APP, "logs", "toggle_commands.log"))
CMDS = {"applied": 0, "rejected": 0, "last": None, "subscribed": False, "force": False}


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, flush=True)


def seal(state):
    iv = secrets.token_bytes(12)
    return b"S1" + iv + AES.encrypt(iv, gzip.compress(json.dumps(state, separators=(",", ":")).encode(), 6), b"sentinel")


def trade_sig(s):
    return json.dumps({k: [len(a.get("entries", [])), len(a.get("closes", [])), [h.get("id") for h in a.get("holdings", [])], a.get("halt"), a.get("switches")]
                       for k, a in (s.get("accounts") or {}).items()}, sort_keys=True)


connected = {"ok": False}


def on_connect(cl, u, f, rc, p=None):
    connected["ok"] = not rc.is_failure
    log(f"mqtt connected rc={rc}")
    if not rc.is_failure:
        cl.subscribe(CMD_TOPIC, qos=1)
        CMDS["subscribed"] = True


def on_message(cl, u, msg):
    if msg.topic != CMD_TOPIC:
        return
    ok, why = HANDLER.handle(msg.payload)
    CMDS["applied" if ok else "rejected"] += 1
    CMDS["last"] = {"ts": time.time(), "ok": ok, "why": why}
    CMDS["force"] = True
    log(f"command {'applied' if ok else 'rejected: ' + why}")


def main():
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="sp-" + secrets.token_hex(6), transport="websockets")
    c.tls_set(); c.ws_set_options(path=WSPATH); c.username_pw_set("public", "public")
    c.reconnect_delay_set(2, 60)
    c.on_connect = on_connect
    c.on_message = on_message
    c.on_disconnect = lambda cl, u, f, rc, p=None: (connected.__setitem__("ok", False), CMDS.__setitem__("subscribed", False), log(f"mqtt disconnected rc={rc}"))
    c.connect_async(BROKER, PORT, keepalive=30); c.loop_start()
    last_pub, last_sig, last_ntfy, n_pub, n_ntfy, errs = 0.0, None, 0.0, 0, 0, 0
    sess = requests.Session()
    log(f"publisher up, commands on, ntfy every {NTFY_EVERY}s")
    while True:
        t = time.time()
        try:
            s = sess.get(f"http://127.0.0.1:{APP_PORT}/api/state?t={TOKEN}", timeout=10).json()
        except Exception as e:
            s = None; errs += 1
            if errs % 12 == 1: log(f"local app not reachable: {type(e).__name__}")
        if s and s.get("accounts"):
            s["relay"] = {"commands": CMDS["subscribed"], "applied": CMDS["applied"], "rejected": CMDS["rejected"], "last": CMDS["last"]}
            sig = trade_sig(s)
            due = (sig != last_sig and t - last_pub >= MIN_GAP) or t - last_pub >= HEARTBEAT or CMDS["force"]
            if due and connected["ok"]:
                blob = seal(s)
                r = c.publish(TOPIC, blob, qos=1, retain=True)
                if r.rc == mqtt.MQTT_ERR_SUCCESS:
                    last_pub, last_sig, n_pub = t, sig, n_pub + 1
                    CMDS["force"] = False
                else:
                    log(f"mqtt publish rc={r.rc}")
            if t - last_ntfy >= NTFY_EVERY:
                last_ntfy = t
                try:
                    r = sess.put(f"https://ntfy.sh/{NTFY_TOPIC}", data=seal(s), headers={"Filename": "s.bin", "Cache": "yes"}, timeout=20)
                    if r.ok: n_ntfy += 1
                    else: log(f"ntfy {r.status_code} {r.text[:120]}")
                except Exception as e:
                    log(f"ntfy error {e!r}")
            try:
                json.dump({"ts": t, "mqtt_connected": connected["ok"], "last_publish": last_pub, "published": n_pub, "ntfy_published": n_ntfy,
                           "last_ntfy": last_ntfy, "commands_subscribed": CMDS["subscribed"], "commands_applied": CMDS["applied"],
                           "commands_rejected": CMDS["rejected"]}, open(STATUS + ".tmp", "w"))
                os.replace(STATUS + ".tmp", STATUS)
            except OSError:
                pass
        time.sleep(max(0.5, 1.0 - (time.time() - t)) if CMDS["force"] else max(0.5, 2.0 - (time.time() - t)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
