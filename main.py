import os
import time
import subprocess
import sqlite3
import logging
import threading
import json
import smtplib
import requests
import traceback
from email.mime.text import MIMEText
from email.header import Header
from functools import wraps
from flask import Flask, render_template_string, request, redirect, url_for, session, flash
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# --- 基础配置 ---
WATCH_DIR = "/watchdir"
DATA_DIR = "/app/data"
DB_PATH = os.path.join(DATA_DIR, "uploads.db")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
RCLONE_CONF = "/root/.config/rclone/rclone.conf"
RCLONE_LOG_FILE = os.path.join(DATA_DIR, "rclone.log")

app = Flask(__name__)
app.secret_key = os.urandom(24)

# --- 日志配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger()

# --- 默认设置 ---
DEFAULT_SETTINGS = {
    "check_duration": 10,
    "prevent_reupload": True,
    "auto_delete": True,
    "rclone_remote": "",
    "rclone_path": "/",
    "rclone_buffer": "64M",
    "rclone_transfers": "4",
    "rclone_checkers": "8",
    "notify_email_enable": False,
    "smtp_server": "smtp.qq.com",
    "smtp_port": 465,
    "smtp_user": "",
    "smtp_pass": "",
    "email_to": "",
    "notify_bark_enable": False,
    "bark_url": "",
    "notify_wechat_enable": False,
    "wechat_key": ""
}

# --- 工具函数 ---
def load_settings():
    settings = DEFAULT_SETTINGS.copy()
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r') as f:
                saved = json.load(f)
                settings.update(saved)
        except: pass
    if not settings['rclone_remote']:
        env_remote = os.getenv('RCLONE_REMOTE', '')
        if env_remote:
            parts = env_remote.split(':', 1)
            settings['rclone_remote'] = parts[0] + ":"
            settings['rclone_path'] = parts[1] if len(parts) > 1 else "/"
    return settings

def save_settings(new_settings):
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(new_settings, f, indent=4)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS history
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  filename TEXT, size INTEGER, upload_time TEXT, status TEXT,
                  UNIQUE(filename, size))''')
    conn.commit()
    conn.close()

def get_rclone_remotes():
    try:
        res = subprocess.run(["rclone", "listremotes", "--config", RCLONE_CONF], capture_output=True, text=True)
        return [r.strip() for r in res.stdout.split('\n') if r.strip()]
    except: return []

# --- 通知系统 ---
def send_notification(title, content):
    s = load_settings()
    if s['notify_email_enable'] and s['smtp_user'] and s['email_to']:
        try:
            msg = MIMEText(content, 'plain', 'utf-8')
            msg['From'] = s['smtp_user']
            msg['To'] = s['email_to']
            msg['Subject'] = Header(title, 'utf-8')
            smtp = smtplib.SMTP_SSL(s['smtp_server'], int(s['smtp_port']))
            smtp.login(s['smtp_user'], s['smtp_pass'])
            smtp.sendmail(s['smtp_user'], [s['email_to']], msg.as_string())
            smtp.quit()
        except Exception as e: logger.error(f"邮件失败: {e}")

    if s['notify_bark_enable'] and s['bark_url']:
        try: requests.get(f"{s['bark_url']}/{title}/{content}", timeout=5)
        except Exception as e: logger.error(f"Bark失败: {e}")

    if s['notify_wechat_enable'] and s['wechat_key']:
        try: requests.post(f"https://sctapi.ftqq.com/{s['wechat_key']}.send", data={'title': title, 'desp': content}, timeout=5)
        except Exception as e: logger.error(f"微信失败: {e}")

# --- 核心逻辑 ---
def is_file_free(filepath, duration):
    try:
        size1 = os.path.getsize(filepath)
        time.sleep(duration)
        size2 = os.path.getsize(filepath)
        return size1 == size2
    except: return False

def process_file(filepath):
    if not os.path.exists(filepath): return
    filename = os.path.basename(filepath)
    if filename.endswith(('.tmp', '.aria2', '.part', '.downloading', '.ds_store')): return

    s = load_settings()
    filesize = os.path.getsize(filepath)

    if s['prevent_reupload']:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT * FROM history WHERE filename=? AND size=? AND status='success'", (filename, filesize))
        if cur.fetchone():
            logger.info(f"🚫 [防重] 跳过: {filename}")
            conn.close()
            if s['auto_delete']:
                try: os.remove(filepath)
                except: pass
            return
        conn.close()

    logger.info(f"🔍 [校验] {filename}")
    if not is_file_free(filepath, int(s['check_duration'])):
        logger.info(f"⏳ [等待] 文件写入中: {filename}")
        return

    remote = s['rclone_remote']
    if not remote: return

    full_remote = f"{remote}{s['rclone_path']}"
    cmd = ["rclone", "copy", filepath, full_remote,
           "--buffer-size", str(s['rclone_buffer']),
           "--transfers", str(s['rclone_transfers']),
           "--checkers", str(s['rclone_checkers']),
           "--log-file", RCLONE_LOG_FILE, "--log-level", "INFO"]

    logger.info(f"🚀 [上传] {filename}")
    try:
        start_time = time.time()
        result = subprocess.run(cmd)
        duration = round(time.time() - start_time, 2)
        status = "success" if result.returncode == 0 else "failed"
        
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT OR REPLACE INTO history (filename, size, upload_time, status) VALUES (?, ?, ?, ?)", 
                     (filename, filesize, time.strftime('%Y-%m-%d %H:%M:%S'), status))
        conn.commit()
        conn.close()

        if status == "success":
            logger.info(f"✅ [完成] {filename}")
            send_notification("Rclone上传成功", f"文件: {filename}\n耗时: {duration}s")
            if s['auto_delete']:
                os.remove(filepath)
                try:
                    parent = os.path.dirname(filepath)
                    if not os.listdir(parent) and parent != WATCH_DIR: os.rmdir(parent)
                except: pass
        else:
            logger.error(f"❌ [失败] {filename}")
            send_notification("Rclone上传失败", filename)
    except Exception as e: logger.error(f"异常: {e}")

class Handler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory: threading.Thread(target=process_file, args=(event.src_path,)).start()
    def on_moved(self, event):
        if not event.is_directory: threading.Thread(target=process_file, args=(event.dest_path,)).start()

def start_watcher():
    observer = Observer()
    observer.schedule(Handler(), WATCH_DIR, recursive=True)
    observer.start()

# --- Web UI ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session: return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# HTML 模板片段
HTML_HEADER = """
<!DOCTYPE html>
<html lang="zh-CN" data-bs-theme="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>飞牛 NAS Rclone Pro</title>
    <link href="https://cdn.bootcdn.net/ajax/libs/bootswatch/5.3.0/darkly/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.bootcdn.net/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <style>
        body { font-family: 'Segoe UI', system-ui; background-color: #1a1a1a; padding-bottom: 60px; }
        .navbar { background-color: #222 !important; box-shadow: 0 2px 10px rgba(0,0,0,0.3); }
        .card { border: none; background-color: #2b2b2b; margin-bottom: 20px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.2); }
        .card-header { background-color: #323232; border-bottom: 1px solid #444; font-weight: 600; padding: 15px 20px; border-radius: 12px 12px 0 0 !important; }
        .log-box { background: #000; color: #0f0; font-family: monospace; height: 500px; overflow-y: auto; padding: 15px; border-radius: 8px; font-size: 13px; }
        ::-webkit-scrollbar { width: 8px; }
        ::-webkit-scrollbar-track { background: #222; }
        ::-webkit-scrollbar-thumb { background: #555; border-radius: 4px; }
    </style>
</head>
<body>
<nav class="navbar navbar-expand-lg navbar-dark mb-4 sticky-top">
  <div class="container">
    <a class="navbar-brand text-primary fw-bold" href="/"><i class="fa-solid fa-rocket me-2"></i>Rclone Pro</a>
    <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#nav"><span class="navbar-toggler-icon"></span></button>
    <div class="collapse navbar-collapse" id="nav">
      <ul class="navbar-nav ms-auto">
        <li class="nav-item"><a class="nav-link" href="/"><i class="fa-solid fa-gauge me-1"></i>仪表盘</a></li>
        <li class="nav-item"><a class="nav-link" href="/history"><i class="fa-solid fa-list-check me-1"></i>清单</a></li>
        <li class="nav-item"><a class="nav-link" href="/settings"><i class="fa-solid fa-gear me-1"></i>设置</a></li>
        <li class="nav-item"><a class="nav-link" href="/edit_conf"><i class="fa-solid fa-file-code me-1"></i>配置编辑</a></li>
        <li class="nav-item"><a class="nav-link text-danger" href="/logout"><i class="fa-solid fa-right-from-bracket"></i></a></li>
      </ul>
    </div>
  </div>
</nav>
<div class="container fade-in">
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        {% for cat, msg in messages %}
          <div class="alert alert-{{ cat }} alert-dismissible fade show shadow-sm"><i class="fa-solid fa-circle-info me-2"></i>{{ msg }}<button type="button" class="btn-close" data-bs-dismiss="alert"></button></div>
        {% endfor %}
      {% endif %}
    {% endwith %}
"""

HTML_FOOTER = """
</div>
<script src="https://cdn.bootcdn.net/ajax/libs/bootstrap/5.3.0/js/bootstrap.bundle.min.js"></script>
<script>const lb = document.querySelector('.log-box'); if(lb) lb.scrollTop = lb.scrollHeight;</script>
</body></html>
"""

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form['password'] == os.getenv('PANEL_PASSWORD', '123456'):
            session['logged_in'] = True
            return redirect(url_for('dashboard'))
        flash('密码错误', 'danger')
    return render_template_string("""
    <!DOCTYPE html><html data-bs-theme="dark"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
    <link href="https://cdn.bootcdn.net/ajax/libs/bootswatch/5.3.0/darkly/bootstrap.min.css" rel="stylesheet">
    <style>body{height:100vh;display:flex;align-items:center;justify-content:center;background:#1a1a1a}</style></head>
    <body><div class="card p-4 shadow-lg" style="width:350px"><h3 class="text-center mb-4 text-primary">🚀 Rclone Pro</h3>
    <form method="post"><input type="password" name="password" class="form-control mb-3" placeholder="密码" required>
    <button class="btn btn-primary w-100">登录</button></form></div></body></html>
    """)

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

@app.route('/')
@login_required
def dashboard():
    logs = "加载日志..."
    if os.path.exists(RCLONE_LOG_FILE):
        try: with open(RCLONE_LOG_FILE, 'r') as f: logs = f.read()[-8000:]
        except: pass
    s = load_settings()
    
    content = """
    <div class="row g-4">
        <div class="col-lg-4">
            <div class="card h-100">
                <div class="card-header"><i class="fa-solid fa-server me-2"></i>运行状态</div>
                <div class="card-body">
                    <div class="d-flex justify-content-between mb-3 border-bottom pb-2"><span>系统状态</span><span class="badge bg-success rounded-pill">运行中</span></div>
                    <div class="mb-3"><label class="text-muted small">远程仓库</label><div class="text-info fw-bold">{{ s['rclone_remote'] or '未配置' }}</div></div>
                    <div class="mb-3"><label class="text-muted small">上传路径</label><div class="text-warning font-monospace">{{ s['rclone_path'] }}</div></div>
                    <div class="mb-3"><label class="text-muted small">Web端口</label><div>{{ port }}</div></div>
                     <div class="d-grid gap-2"><a href="/settings" class="btn btn-outline-primary"><i class="fa-solid fa-gear"></i> 修改配置</a></div>
                </div>
            </div>
        </div>
        <div class="col-lg-8">
            <div class="card h-100">
                <div class="card-header d-flex justify-content-between align-items-center"><span><i class="fa-solid fa-terminal me-2"></i>实时日志</span><a href="/" class="btn btn-sm btn-dark"><i class="fa-solid fa-rotate"></i></a></div>
                <div class="card-body p-0"><div class="log-box">{{ logs }}</div></div>
            </div>
        </div>
    </div>
    """
    return render_template_string(HTML_HEADER + content + HTML_FOOTER, logs=logs, s=s, port=request.host.split(':')[-1])

@app.route('/history')
@login_required
def history():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM history ORDER BY id DESC LIMIT 100").fetchall()
    conn.close()
    content = """
    <div class="card">
        <div class="card-header d-flex justify-content-between align-items-center"><span><i class="fa-solid fa-clock-rotate-left me-2"></i>最近 100 条记录</span>
        <form action="/clear_history" method="post" onsubmit="return confirm('确定清空？')"><button class="btn btn-sm btn-danger"><i class="fa-solid fa-trash"></i> 清空</button></form></div>
        <div class="table-responsive"><table class="table table-hover table-striped mb-0 align-middle"><thead><tr><th>文件</th><th>大小</th><th>时间</th><th>状态</th></tr></thead><tbody>
        {% for r in rows %}<tr><td><div class="text-truncate" style="max-width:200px" title="{{ r['filename'] }}">{{ r['filename'] }}</div></td>
        <td>{{ (r['size']/1024/1024)|round(2) }} MB</td><td class="small text-muted">{{ r['upload_time'] }}</td>
        <td><span class="badge bg-{{ 'success' if r['status']=='success' else 'danger' }}">{{ '成功' if r['status']=='success' else '失败' }}</span></td></tr>{% endfor %}
        </tbody></table></div>
    </div>
    """
    return render_template_string(HTML_HEADER + content + HTML_FOOTER, rows=rows)

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        if 'test_email' in request.form:
            send_notification("Rclone Pro 测试", "测试邮件成功！")
            flash('测试邮件已发送', 'info')
            return redirect(url_for('settings'))
        
        new_s = {
            "check_duration": request.form.get('check_duration', 10),
            "prevent_reupload": 'prevent_reupload' in request.form,
            "auto_delete": 'auto_delete' in request.form,
            "rclone_remote": request.form.get('rclone_remote', ''),
            "rclone_path": request.form.get('rclone_path', '/'),
            "rclone_buffer": request.form.get('rclone_buffer', '64M'),
            "rclone_transfers": request.form.get('rclone_transfers', '4'),
            "rclone_checkers": request.form.get('rclone_checkers', '8'),
            "notify_email_enable": 'notify_email_enable' in request.form,
            "smtp_server": request.form.get('smtp_server', ''),
            "smtp_port": request.form.get('smtp_port', 465),
            "smtp_user": request.form.get('smtp_user', ''),
            "smtp_pass": request.form.get('smtp_pass', ''),
            "email_to": request.form.get('email_to', ''),
            "notify_bark_enable": 'notify_bark_enable' in request.form,
            "bark_url": request.form.get('bark_url', ''),
            "notify_wechat_enable": 'notify_wechat_enable' in request.form,
            "wechat_key": request.form.get('wechat_key', '')
        }
        save_settings(new_s)
        flash('配置已保存', 'success')
        return redirect(url_for('settings'))
    
    s = load_settings()
    remotes = get_rclone_remotes()
    content = """
    <div class="row justify-content-center"><div class="col-lg-10"><form method="post">
    <ul class="nav nav-pills mb-4 nav-justified"><li class="nav-item"><button class="nav-link active" data-bs-toggle="pill" data-bs-target="#tab-basic" type="button">基础 & Rclone</button></li>
    <li class="nav-item"><button class="nav-link" data-bs-toggle="pill" data-bs-target="#tab-notify" type="button">通知设置</button></li></ul>
    <div class="tab-content"><div class="tab-pane fade show active" id="tab-basic">
        <div class="card mb-4"><div class="card-header text-primary">核心策略</div><div class="card-body"><div class="row">
            <div class="col-md-6 mb-3"><div class="form-check form-switch p-3 border rounded"><input class="form-check-input" type="checkbox" name="prevent_reupload" {% if s['prevent_reupload'] %}checked{% endif %}><label class="form-check-label fw-bold">防重复上传</label></div></div>
            <div class="col-md-6 mb-3"><div class="form-check form-switch p-3 border rounded"><input class="form-check-input" type="checkbox" name="auto_delete" {% if s['auto_delete'] %}checked{% endif %}><label class="form-check-label fw-bold">自动清理本地</label></div></div></div>
            <div class="mb-3"><label>检测时长(秒)</label><input type="number" name="check_duration" class="form-control" value="{{ s['check_duration'] }}"></div></div></div>
        <div class="card"><div class="card-header text-warning">Rclone 配置</div><div class="card-body">
            <div class="mb-3"><label>远程仓库 (Remote)</label><div class="input-group"><select name="rclone_remote" class="form-select"><option value="">-- 请选择 --</option>{% for r in remotes %}<option value="{{ r }}" {% if s['rclone_remote'] == r %}selected{% endif %}>{{ r }}</option>{% endfor %}</select><a href="/edit_conf" class="btn btn-outline-secondary">新建/编辑</a></div></div>
            <div class="mb-3"><label>上传路径</label><input type="text" name="rclone_path" class="form-control font-monospace" value="{{ s['rclone_path'] }}"></div>
            <div class="row"><div class="col-4"><label>缓冲区</label><input type="text" name="rclone_buffer" class="form-control" value="{{ s['rclone_buffer'] }}"></div>
            <div class="col-4"><label>并发数</label><input type="number" name="rclone_transfers" class="form-control" value="{{ s['rclone_transfers'] }}"></div>
            <div class="col-4"><label>检查器</label><input type="number" name="rclone_checkers" class="form-control" value="{{ s['rclone_checkers'] }}"></div></div></div></div>
    </div>
    <div class="tab-pane fade" id="tab-notify">
        <div class="card mb-3"><div class="card-header">邮箱通知</div><div class="card-body"><div class="row g-3">
            <div class="col-12"><div class="form-check form-switch"><input class="form-check-input" type="checkbox" name="notify_email_enable" {% if s['notify_email_enable'] %}checked{% endif %}><label>启用</label></div></div>
            <div class="col-md-8"><input type="text" name="smtp_server" class="form-control" placeholder="SMTP服务器" value="{{ s['smtp_server'] }}"></div>
            <div class="col-md-4"><input type="number" name="smtp_port" class="form-control" placeholder="端口" value="{{ s['smtp_port'] }}"></div>
            <div class="col-md-6"><input type="text" name="smtp_user" class="form-control" placeholder="账号" value="{{ s['smtp_user'] }}"></div>
            <div class="col-md-6"><input type="password" name="smtp_pass" class="form-control" placeholder="密码" value="{{ s['smtp_pass'] }}"></div>
            <div class="col-12"><input type="email" name="email_to" class="form-control" placeholder="收件人" value="{{ s['email_to'] }}"></div>
            <div class="col-12"><button type="submit" name="test_email" value="1" class="btn btn-sm btn-outline-info w-100">发送测试</button></div></div></div></div>
        <div class="card mb-3"><div class="card-header">Bark 推送</div><div class="card-body"><div class="form-check form-switch mb-2"><input class="form-check-input" type="checkbox" name="notify_bark_enable" {% if s['notify_bark_enable'] %}checked{% endif %}><label>启用</label></div><input type="text" name="bark_url" class="form-control" placeholder="Bark URL" value="{{ s['bark_url'] }}"></div></div>
        <div class="card mb-3"><div class="card-header">微信通知</div><div class="card-body"><div class="form-check form-switch mb-2"><input class="form-check-input" type="checkbox" name="notify_wechat_enable" {% if s['notify_wechat_enable'] %}checked{% endif %}><label>启用</label></div><input type="text" name="wechat_key" class="form-control" placeholder="Server酱 Key" value="{{ s['wechat_key'] }}"></div></div>
    </div></div>
    <div class="d-grid gap-2 mt-4 pb-5"><button type="submit" class="btn btn-primary btn-lg fw-bold"><i class="fa-solid fa-floppy-disk me-2"></i>保存所有配置</button></div>
    </form></div></div>
    """
    return render_template_string(HTML_HEADER + content + HTML_FOOTER, s=s, remotes=remotes)

@app.route('/edit_conf', methods=['GET', 'POST'])
@login_required
def edit_conf():
    if request.method == 'POST':
        try:
            with open(RCLONE_CONF, 'w') as f: f.write(request.form.get('content'))
            flash('保存成功', 'success')
        except Exception as e: flash(f'失败: {e}', 'danger')
        return redirect(url_for('edit_conf'))
    content = ""
    if os.path.exists(RCLONE_CONF):
        with open(RCLONE_CONF, 'r') as f: content = f.read()
    html_content = """
    <div class="card h-100"><div class="card-header d-flex justify-content-between align-items-center"><span><i class="fa-solid fa-file-pen me-2"></i>编辑 rclone.conf</span><span class="badge bg-warning text-dark">慎重修改</span></div>
    <div class="card-body"><form method="post"><div class="mb-3"><textarea name="content" class="form-control bg-dark text-success font-monospace" rows="20" spellcheck="false">{{ c }}</textarea></div>
    <div class="d-flex justify-content-between"><a href="/settings" class="btn btn-secondary">返回</a><button type="submit" class="btn btn-success">保存</button></div></form></div></div>
    """
    return render_template_string(HTML_HEADER + html_content + HTML_FOOTER, c=content)

@app.route('/clear_history', methods=['POST'])
@login_required
def clear_history():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM history")
    conn.commit()
    conn.close()
    flash('已清空', 'warning')
    return redirect(url_for('history'))

if __name__ == "__main__":
    # --- 终极防闪退逻辑 ---
    try:
        init_db()
        start_watcher()
        port = int(os.getenv('PANEL_PORT', 5572))
        print(f"✅ 面板启动成功: http://0.0.0.0:{port}")
        app.run(host='0.0.0.0', port=port)
    except Exception as e:
        # 如果报错，打印错误并挂起，防止 Docker 无限重启
        print(f"❌ 严重错误: {e}")
        traceback.print_exc()
        print("🛑 正在挂起容器以便调试... (请检查日志)")
        while True: time.sleep(100)
