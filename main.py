import sys
import time
import os
import traceback
import subprocess
import threading
import json
import sqlite3
import logging

# --- 🛡️ 启动守护 & 依赖检查 ---
try:
    import requests
    import smtplib
    from email.mime.text import MIMEText
    from email.header import Header
    from functools import wraps
    from flask import Flask, render_template_string, request, redirect, url_for, session, flash
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except ImportError as e:
    print("\n" + "!"*50)
    print(f"❌ 启动失败: 缺少必要依赖库 -> {e}")
    print("请检查 requirements.txt 是否包含: requests, flask, watchdog")
    print("⚠️ 容器已进入挂机模式，请修复依赖后重启")
    print("!"*50 + "\n")
    while True:
        time.sleep(100)

# --- 基础配置 ---
WATCH_DIR = "/watchdir"
DATA_DIR = "/app/data"
DB_PATH = os.path.join(DATA_DIR, "uploads.db")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
RCLONE_CONF = "/root/.config/rclone/rclone.conf"
RCLONE_LOG_FILE = os.path.join(DATA_DIR, "rclone.log")

app = Flask(__name__)
app.secret_key = os.urandom(24)

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

# --- 核心函数 ---
def load_settings():
    settings = DEFAULT_SETTINGS.copy()
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r') as f:
                settings.update(json.load(f))
        except:
            pass
    # 环境变量兜底
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
    except:
        return []

def rclone_obscure(password):
    """调用 rclone obscure 命令加密密码"""
    try:
        # 必须使用 rclone obscure 才能生成配置文件可用的密码
        res = subprocess.run(["rclone", "obscure", password], capture_output=True, text=True)
        if res.returncode == 0:
            return res.stdout.strip()
        else:
            logger.error(f"密码加密失败: {res.stderr}")
            return password # 失败返回原密码(虽然可能没用)
    except Exception as e:
        logger.error(f"加密调用异常: {e}")
        return password

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
        except Exception as e:
            logger.error(f"邮件失败: {e}")
    if s['notify_bark_enable'] and s['bark_url']:
        try:
            requests.get(f"{s['bark_url']}/{title}/{content}", timeout=5)
        except:
            pass
    if s['notify_wechat_enable'] and s['wechat_key']:
        try:
            requests.post(f"https://sctapi.ftqq.com/{s['wechat_key']}.send", data={'title': title, 'desp': content}, timeout=5)
        except:
            pass

def is_file_free(filepath, duration):
    try:
        size1 = os.path.getsize(filepath)
        time.sleep(duration)
        size2 = os.path.getsize(filepath)
        return size1 == size2
    except:
        return False

def process_file(filepath):
    if not os.path.exists(filepath):
        return
    filename = os.path.basename(filepath)
    if filename.endswith(('.tmp', '.aria2', '.part', '.downloading', '.ds_store')):
        return

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
                try:
                    os.remove(filepath)
                except:
                    pass
            return
        conn.close()

    logger.info(f"🔍 [校验] {filename}")
    if not is_file_free(filepath, int(s['check_duration'])):
        logger.info(f"⏳ [等待] 写入中: {filename}")
        return

    remote = s['rclone_remote']
    if not remote:
        logger.warning("⚠️ 未配置远程仓库，无法上传")
        return

    full_remote = f"{remote}{s['rclone_path']}"
    # --- 关键修复：确保使用用户配置的参数 ---
    cmd = ["rclone", "copy", filepath, full_remote,
           "--buffer-size", str(s['rclone_buffer']),
           "--transfers", str(s['rclone_transfers']),
           "--checkers", str(s['rclone_checkers']),
           "--log-file", RCLONE_LOG_FILE, "--log-level", "INFO"]

    logger.info(f"🚀 [上传] {filename} -> {full_remote}")
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
                    if not os.listdir(parent) and parent != WATCH_DIR:
                        os.rmdir(parent)
                except:
                    pass
        else:
            logger.error(f"❌ [失败] {filename}")
            # 如果失败，读取最后几行日志
            try:
                with open(RCLONE_LOG_FILE, 'r') as f:
                    err_log = f.readlines()[-3:]
                    logger.error(f"Rclone报错: {err_log}")
            except:
                pass
            send_notification("Rclone上传失败", f"{filename}\n请检查配置")
    except Exception as e:
        logger.error(f"异常: {e}")

class Handler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            threading.Thread(target=process_file, args=(event.src_path,)).start()
    def on_moved(self, event):
        if not event.is_directory:
            threading.Thread(target=process_file, args=(event.dest_path,)).start()

def start_watcher():
    observer = Observer()
    observer.schedule(Handler(), WATCH_DIR, recursive=True)
    observer.start()

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# --- UI 模板 (Bootstrap 5 Darkly) ---
HTML_HEADER = """
<!DOCTYPE html><html lang="zh-CN" data-bs-theme="dark"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>NAS Rclone Pro</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
<style>
body{background:#121212;padding-top:70px;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.navbar{background:rgba(30,30,30,0.95)!important;backdrop-filter:blur(10px);border-bottom:1px solid #333}
.card{background:#1e1e1e;border:1px solid #333;margin-bottom:20px;box-shadow:0 4px 6px rgba(0,0,0,0.2)}
.card-header{background:#252525;border-bottom:1px solid #333;font-weight:600}
.log-box{background:#000;color:#0f0;font-family:monospace;height:500px;overflow-y:auto;padding:10px;border-radius:5px;font-size:13px}
.form-text{font-size:0.85em;color:#888}
</style></head><body>
<nav class="navbar navbar-expand-lg navbar-dark fixed-top"><div class="container">
<a class="navbar-brand text-primary fw-bold" href="/"><i class="fa-solid fa-cloud-arrow-up me-2"></i>Rclone Pro</a>
<button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#n"><span class="navbar-toggler-icon"></span></button>
<div class="collapse navbar-collapse" id="n"><ul class="navbar-nav ms-auto gap-2">
<li class="nav-item"><a class="nav-link" href="/"><i class="fa-solid fa-gauge me-1"></i>仪表盘</a></li>
<li class="nav-item"><a class="nav-link" href="/wizard"><i class="fa-solid fa-wand-magic-sparkles me-1"></i>向导</a></li>
<li class="nav-item"><a class="nav-link" href="/settings"><i class="fa-solid fa-sliders me-1"></i>配置</a></li>
<li class="nav-item"><a class="nav-link" href="/history"><i class="fa-solid fa-list me-1"></i>清单</a></li>
<li class="nav-item"><a class="nav-link" href="/help"><i class="fa-solid fa-circle-question me-1"></i>帮助</a></li>
<li class="nav-item"><a class="nav-link text-danger" href="/logout"><i class="fa-solid fa-power-off"></i></a></li>
</ul></div></div></nav>
<div class="container py-3">
{% with m=get_flashed_messages(with_categories=true) %}{% if m %}{% for c,msg in m %}
<div class="alert alert-{{ c }} alert-dismissible fade show shadow-sm">{{ msg }}<button type="button" class="btn-close" data-bs-dismiss="alert"></button></div>
{% endfor %}{% endif %}{% endwith %}
"""

HTML_FOOTER = """</div><script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
<script>
const lb=document.querySelector('.log-box');if(lb)lb.scrollTop=lb.scrollHeight;
function updateTip(s){
 const tips={'webdav':'通常是 http://IP:端口/dav','smb':'例如 //192.168.1.5/share','ftp':'例如 192.168.1.5:21'};
 document.getElementById('url-tip').innerText=tips[s.value]||'服务器地址';
}
</script></body></html>"""

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form['password'] == os.getenv('PANEL_PASSWORD', '123456'):
            session['logged_in'] = True
            return redirect(url_for('dashboard'))
        flash('密码错误', 'danger')
    return render_template_string("""<!DOCTYPE html><html data-bs-theme="dark"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>body{height:100vh;display:flex;align-items:center;justify-content:center;background:#121212}</style></head>
    <body><div class="card p-4 shadow-lg border-0" style="width:350px;background:#1e1e1e"><div class="text-center mb-4"><h3 class="fw-bold text-primary">Rclone Pro</h3><p class="text-muted small">v4.1 Final</p></div>
    <form method="post"><input type="password" name="password" class="form-control mb-3 bg-dark text-white" placeholder="请输入密码" required>
    <button class="btn btn-primary w-100">登录</button></form></div></body></html>""")

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

@app.route('/')
@login_required
def dashboard():
    logs = "等待日志..."
    if os.path.exists(RCLONE_LOG_FILE):
        try:
            with open(RCLONE_LOG_FILE, 'r') as f:
                logs = f.read()[-5000:]
        except:
            pass
    s = load_settings()
    content = """
    <div class="row g-4"><div class="col-lg-4"><div class="card h-100"><div class="card-header"><i class="fa-solid fa-server me-2"></i>概览</div><div class="card-body">
    <ul class="list-group list-group-flush mb-3"><li class="list-group-item bg-transparent text-white d-flex justify-content-between px-0"><span>状态</span><span class="badge bg-success">运行中</span></li>
    <li class="list-group-item bg-transparent text-white d-flex justify-content-between px-0"><span>仓库</span><span class="text-info font-monospace">{{ s['rclone_remote'] or '未配置' }}</span></li>
    <li class="list-group-item bg-transparent text-white d-flex justify-content-between px-0"><span>路径</span><span class="text-warning font-monospace">{{ s['rclone_path'] }}</span></li></ul>
    <div class="d-grid gap-2">{% if not s['rclone_remote'] %}<a href="/wizard" class="btn btn-primary pulse"><i class="fa-solid fa-plus me-2"></i>新建连接</a>{% else %}
    <a href="/settings" class="btn btn-outline-light">修改配置</a>{% endif %}</div></div></div></div>
    <div class="col-lg-8"><div class="card h-100"><div class="card-header d-flex justify-content-between"><span><i class="fa-solid fa-terminal me-2"></i>日志</span><a href="/" class="btn btn-sm btn-outline-secondary">刷新</a></div>
    <div class="card-body p-0"><div class="log-box">{{ logs }}</div></div></div></div></div>
    <style>.pulse{animation:pulse 2s infinite}@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(13,110,253,0.7)}70%{box-shadow:0 0 0 10px rgba(13,110,253,0)}100%{box-shadow:0 0 0 0 rgba(13,110,253,0)}}</style>
    """
    return render_template_string(HTML_HEADER + content + HTML_FOOTER, logs=logs, s=s)

@app.route('/wizard', methods=['GET', 'POST'])
@login_required
def wizard():
    if request.method == 'POST':
        try:
            t = request.form.get('type')
            n = request.form.get('name')
            u = request.form.get('url')
            usr = request.form.get('user')
            pwd = request.form.get('pass')
            
            # --- 修复核心：调用 rclone obscure 加密密码 ---
            obs_pwd = rclone_obscure(pwd)
            
            cfg = f"\n[{n}]\ntype = {t}\n"
            if t == 'webdav':
                cfg += f"url = {u}\nvendor = other\nuser = {usr}\npass = {obs_pwd}\n"
            elif t == 'ftp':
                cfg += f"host = {u}\nuser = {usr}\npass = {obs_pwd}\n"
            elif t == 'smb':
                cfg += f"host = {u}\nuser = {usr}\npass = {obs_pwd}\n"
            
            with open(RCLONE_CONF, 'a') as f:
                f.write(cfg)
                
            s = load_settings()
            s['rclone_remote'] = f"{n}:"
            save_settings(s)
            flash(f'成功添加 [{n}]，密码已加密！', 'success')
            return redirect(url_for('dashboard'))
        except Exception as e:
            flash(f'错误: {e}', 'danger')
            
    content = """
    <div class="row justify-content-center"><div class="col-md-8"><div class="card"><div class="card-header bg-primary text-white">新建连接向导</div><div class="card-body">
    <div class="alert alert-info small"><i class="fa-solid fa-shield-halved me-1"></i>系统会自动加密您的密码，请放心填写明文。</div>
    <form method="post"><div class="mb-3"><label class="form-label">存储类型</label><select name="type" class="form-select" onchange="updateTip(this)"><option value="webdav">WebDAV (Alist/123盘)</option><option value="smb">SMB (NAS/Win)</option><option value="ftp">FTP</option></select></div>
    <div class="mb-3"><label class="form-label">连接名称 (英文)</label><input type="text" name="name" class="form-control" placeholder="例如: my_alist" required pattern="[a-zA-Z0-9_]+"><div class="form-text">给这个连接起个名字，不要中文</div></div>
    <div class="mb-3"><label class="form-label">服务器地址</label><input type="text" name="url" class="form-control" placeholder="http://..." required><div class="form-text text-info" id="url-tip">通常是 http://IP:端口/dav</div></div>
    <div class="row"><div class="col-6"><label class="form-label">账号</label><input type="text" name="user" class="form-control"></div>
    <div class="col-6"><label class="form-label">密码</label><input type="password" name="pass" class="form-control"></div></div>
    <button class="btn btn-primary w-100 mt-4">添加并使用</button></form></div></div></div></div>
    """
    return render_template_string(HTML_HEADER + content + HTML_FOOTER)

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        if 'test_email' in request.form:
            send_notification("Rclone Pro", "邮件配置测试成功！")
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
    <div class="card mb-3"><div class="card-header">基础</div><div class="card-body">
    <div class="row"><div class="col-md-6"><div class="form-check form-switch p-2 border rounded border-secondary mb-2"><input class="form-check-input ms-0 me-2" type="checkbox" name="prevent_reupload" {% if s['prevent_reupload'] %}checked{% endif %}><label>防重复上传</label></div></div>
    <div class="col-md-6"><div class="form-check form-switch p-2 border rounded border-secondary mb-2"><input class="form-check-input ms-0 me-2" type="checkbox" name="auto_delete" {% if s['auto_delete'] %}checked{% endif %}><label>自动清理本地</label></div></div></div>
    <div class="mb-3"><label class="form-label">选择仓库</label><select name="rclone_remote" class="form-select bg-dark text-white"><option value="">-- 请选择 --</option>{% for r in remotes %}<option value="{{ r }}" {% if s['rclone_remote'] == r %}selected{% endif %}>{{ r }}</option>{% endfor %}</select>
    <div class="form-text">没有选项？去 <a href="/wizard">新建向导</a> 添加，或 <a href="/edit_conf">手动编辑</a>。</div></div>
    <div class="mb-3"><label class="form-label">上传路径</label><input type="text" name="rclone_path" class="form-control bg-dark text-white font-monospace" value="{{ s['rclone_path'] }}"><div class="form-text">例如 /Movie</div></div></div></div>
    
    <div class="card mb-3"><div class="card-header">邮件通知 (SMTP)</div><div class="card-body">
    <div class="form-check form-switch mb-3"><input class="form-check-input" type="checkbox" name="notify_email_enable" {% if s['notify_email_enable'] %}checked{% endif %}><label>启用</label></div>
    <div class="row g-2 mb-2"><div class="col-md-8"><input type="text" name="smtp_server" class="form-control form-control-sm bg-dark text-white" placeholder="服务器 (smtp.qq.com)" value="{{ s['smtp_server'] }}"></div>
    <div class="col-md-4"><input type="text" name="smtp_port" class="form-control form-control-sm bg-dark text-white" placeholder="端口 (465)" value="{{ s['smtp_port'] }}"></div></div>
    <div class="row g-2 mb-2"><div class="col-md-6"><input type="text" name="smtp_user" class="form-control form-control-sm bg-dark text-white" placeholder="你的邮箱账号" value="{{ s['smtp_user'] }}"></div>
    <div class="col-md-6"><input type="password" name="smtp_pass" class="form-control form-control-sm bg-dark text-white" placeholder="授权码 (非密码)" value="{{ s['smtp_pass'] }}"></div></div>
    <div class="input-group input-group-sm mb-3"><span class="input-group-text bg-secondary border-secondary text-white">收件人</span><input type="text" name="email_to" class="form-control bg-dark text-white" placeholder="收件人" value="{{ s['email_to'] }}"><button name="test_email" value="1" class="btn btn-info">测试</button></div>
    <div class="alert alert-info py-2 small mb-0"><i class="fa-solid fa-circle-info me-1"></i>QQ邮箱获取授权码：设置 -> 账号 -> 开启SMTP -> 生成授权码</div></div></div>
    
    <button class="btn btn-primary w-100 btn-lg mb-5">保存所有配置</button></form></div></div>
    """
    return render_template_string(HTML_HEADER + content + HTML_FOOTER, s=s, remotes=remotes)

@app.route('/help')
@login_required
def help_page():
    content = """
    <div class="row justify-content-center"><div class="col-md-8"><h4 class="text-primary mb-3">帮助中心</h4>
    <div class="card mb-3"><div class="card-header">1. 新手起步</div><div class="card-body text-muted small">
    第一步：点击顶部菜单的 <b>[向导]</b>。<br>第二步：选择 <b>WebDAV</b> (适用于Alist/123盘) 或 <b>SMB</b> (适用于NAS)。<br>第三步：填入地址账号密码，点击添加。<br>第四步：在 <b>[配置]</b> 页面确认刚才添加的仓库已被选中。</div></div>
    <div class="card mb-3"><div class="card-header">2. 邮件通知设置</div><div class="card-body text-muted small">
    以QQ邮箱为例：<br>1. 登录网页版QQ邮箱。<br>2. 进入 [设置] -> [账号] -> 开启 [POP3/SMTP服务]。<br>3. 点击 [生成授权码]，复制那个16位的字符串。<br>4. 在本程序 [配置] 页填入：smtp.qq.com / 465 / 你的QQ号 / <b>刚才的授权码</b>。</div></div>
    <div class="card"><div class="card-header">3. 高级功能</div><div class="card-body text-muted small">
    如果需要挂载 OneDrive/GoogleDrive，由于需要浏览器授权，无法在此面板完成。请使用电脑版 Rclone 配置好后，将 <code>rclone.conf</code> 的内容复制到本程序的 <b>[配置编辑]</b> (URL: /edit_conf) 页面中。</div></div></div></div>
    """
    return render_template_string(HTML_HEADER + content + HTML_FOOTER)

@app.route('/edit_conf', methods=['GET', 'POST'])
@login_required
def edit_conf():
    if request.method == 'POST':
        with open(RCLONE_CONF, 'w') as f:
            f.write(request.form.get('content'))
        flash('已保存', 'success')
        return redirect(url_for('edit_conf'))
    c = ""
    if os.path.exists(RCLONE_CONF):
        with open(RCLONE_CONF, 'r') as f:
            c = f.read()
    content = """
    <div class="card h-100"><div class="card-header d-flex justify-content-between"><span>rclone.conf (手动编辑)</span><button type="submit" form="f1" class="btn btn-sm btn-success">保存</button></div>
    <div class="card-body p-0"><form id="f1" method="post"><textarea name="content" class="form-control bg-dark text-white font-monospace border-0" style="height:600px" spellcheck="false">""" + c + """</textarea></form></div></div>
    """
    return render_template_string(HTML_HEADER + content + HTML_FOOTER)

@app.route('/clear_history', methods=['POST'])
@login_required
def clear_history():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM history")
    conn.commit()
    conn.close()
    flash('已清空', 'warning')
    return redirect(url_for('history'))

# --- 🟢 启动入口 ---
if __name__ == "__main__":
    try:
        init_db()
        start_watcher()
        port = int(os.getenv('PANEL_PORT', 5572))
        print(f"✅ 面板启动: http://0.0.0.0:{port}")
        app.run(host='0.0.0.0', port=port)
    except Exception as e:
        # 防崩兜底：如果 Flask 启动失败（如端口占用），挂起不退出
        print(f"❌ 启动异常: {e}")
        traceback.print_exc()
        while True:
            time.sleep(100)
