# ESP32-S3 板端配置服务（MicroPython）
# 作用：板子自己跑一个极简 HTTP 服务，电脑浏览器打开 http://<板子IP>/ 就能改配置、切页面、重启
# 用法：在 main.py 的主循环里调用 WebConfig.handle_once()，不阻塞界面刷新

import json
import usocket as socket


CONFIG_FILE = "config.json"

DEFAULT = {
    "ssid": "",          # WiFi 名
    "pwd": "",           # WiFi 密码
    "page_ms": 30000,    # 每页停留毫秒
    "order": ["stock", "ai"],   # 页面轮播顺序
    "ai_url": "http://192.168.1.10:8080/brief",  # AI 简报接口
    "brightness": 80,    # 屏幕亮度 0-100
}


def load_config():
    try:
        with open(CONFIG_FILE) as f:
            c = json.load(f)
        for k, v in DEFAULT.items():          # 补全新版本新增的字段
            c.setdefault(k, v)
        return c
    except Exception:
        return dict(DEFAULT)                  # 没有配置文件就用默认值


def save_config(c):
    with open(CONFIG_FILE, "w") as f:
        json.dump(c, f)


PAGE_HTML = """<!doctype html><meta charset=utf-8>
<title>面板控制台</title>
<style>
body{font:14px -apple-system,"PingFang SC",system-ui;max-width:520px;margin:40px auto;padding:0 16px;color:#222}
h1{font-size:18px}label{display:block;margin:14px 0 4px;color:#666}
input,select{width:100%;padding:8px;border:1px solid #ddd;border-radius:6px;box-sizing:border-box}
button{margin-top:18px;padding:10px 18px;border:0;border-radius:6px;background:#2f6feb;color:#fff;cursor:pointer}
.row{display:flex;gap:8px;margin-top:16px}
.row button{flex:1;margin:0;background:#444}
#msg{margin-top:14px;color:#1a9d54}
</style>
<h1>面板控制台</h1>
<form id=f>
<label>WiFi 名称<input name=ssid></label>
<label>WiFi 密码<input name=pwd type=password></label>
<label>每页停留毫秒<input name=page_ms type=number></label>
<label>轮播顺序（逗号分隔 stock,ai）<input name=order></label>
<label>AI 简报接口<input name=ai_url></label>
<label>屏幕亮度（0-100）<input name=brightness type=number></label>
<button type=submit>保存并重启</button>
</form>
<div class=row>
<button onclick=act('/api/switch?i=0')>切到股票页</button>
<button onclick=act('/api/switch?i=2')>切到 AI 页</button>
</div>
<div id=msg></div>
<script>
const q=s=>encodeURIComponent(s);
fetch('/api/config').then(r=>r.json()).then(c=>{
  for(const k in c){
    const el=document.querySelector('[name='+k+']');
    if(el) el.value = Array.isArray(c[k]) ? c[k].join(',') : c[k];
  }
});
document.getElementById('f').onsubmit=e=>{e.preventDefault();
  const b=new URLSearchParams(new FormData(e.target)).toString();
  fetch('/api/config',{method:'POST',body:b}).then(r=>msg('已保存，板子正在重启…'));
};
function act(u){fetch(u).then(()=>msg('已切换'));}
function msg(t){document.getElementById('msg').textContent=t;}
</script>
"""


class WebConfig:
    """极简 HTTP 服务。handle_once 每次主循环调用一次，超时 0，绝不卡界面。"""

    def __init__(self, cfg=None, panel=None):
        self.cfg = cfg or load_config()
        self.panel = panel            # 传进 Panel 实例才能远程切页
        self.pending_reboot = False
        self.s = socket.socket()
        self.s.bind(("0.0.0.0", 80))
        self.s.listen(1)

    # ---------- 在主循环里调用 ----------
    def handle_once(self):
        self.s.settimeout(0)               # 非阻塞：没请求立刻返回
        try:
            cl, _ = self.s.accept()
        except Exception:
            return None
        try:
            cl.settimeout(2)
            req = cl.recv(1024).decode()
            self._serve(cl, req)
        except Exception:
            pass
        finally:
            try:
                cl.close()
            except Exception:
                pass
        return self.pending_reboot

    # ---------- 路由 ----------
    def _serve(self, cl, req):
        line = req.split("\r\n")[0] if req else ""
        parts = line.split()
        method = parts[0] if parts else "GET"
        path = parts[1] if len(parts) > 1 else "/"

        body = None
        if "\r\n\r\n" in req:
            body = req.split("\r\n\r\n", 1)[1]

        if path.startswith("/api/switch") and self.panel:
            try:
                i = int(path.split("i=")[1].split("&")[0])
                self.panel.i = i % len(self.panel.pages)
                self.panel.render()
            except Exception:
                pass
            return self._ok(cl, "text/plain", "ok")

        if path.startswith("/api/config"):
            if method == "POST" and body:
                self._apply_form(body)
                save_config(self.cfg)
                self.pending_reboot = True
                return self._ok(cl, "text/plain", "saved")
            return self._ok(cl, "application/json", json.dumps(self.cfg))

        return self._ok(cl, "text/html; charset=utf-8", PAGE_HTML)

    def _apply_form(self, body):
        # 解析 application/x-www-form-urlencoded
        for kv in body.split("&"):
            if "=" not in kv:
                continue
            k, v = kv.split("=", 1)
            k = k.replace("+", " ")
            v = self._unquote(v)
            if k in ("page_ms", "brightness"):
                try:
                    v = int(v)
                except ValueError:
                    continue
            elif k == "order":
                v = [s.strip() for s in v.split(",") if s.strip()]
            self.cfg[k] = v

    @staticmethod
    def _unquote(s):
        out, i = "", 0
        while i < len(s):
            if s[i] == "%" and i + 2 < len(s):
                try:
                    out += chr(int(s[i + 1:i + 3], 16))
                    i += 3
                    continue
                except ValueError:
                    pass
            out += s[i]
            i += 1
        return out

    @staticmethod
    def _ok(cl, ctype, body):
        head = ("HTTP/1.1 200 OK\r\nContent-Type: %s\r\n"
                "Content-Length: %d\r\nConnection: close\r\n\r\n" % (ctype, len(body)))
        cl.send(head.encode() + body.encode())
