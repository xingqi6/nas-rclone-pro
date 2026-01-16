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

# --- 核心逻辑 ---
def is_file_free(filepath, duration):
    try:
        size1 = os.path.getsize(filepath)
        time.sleep(duration)
        size2 = os.path.getsize(filepath)
        return size1 == size2
    except: return False

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
        except: pass
    if s['notify_wechat_enable'] and s['wechat_key']:
        try: requests.post(f"https://sctapi.ftqq.com/{s['wechat_key']}.send", data={'title': title, 'desp': content}, timeout=5)
        except: pass

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

# 增强版 CSS 和 JS
HTML_HEADER = """
<!DOCTYPE html>
<html lang="zh-CN" data-bs-theme="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>NAS Rclone Pro</title>
    <!-- 使用可靠的 CDN -->
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <style>
        :root { --sidebar-width: 260px; --bg-dark: #121212; --card-bg: #1e1e1e; }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; background-color: var(--bg-dark); color: #e0e0e0; padding-top: 60px; }
        
        /* 导航栏美化 */
        .navbar { background-color: rgba(30, 30, 30, 0.95) !important; backdrop-filter: blur(10px); border-bottom: 1px solid #333; z-index: 1030; }
        .navbar-brand { font-weight: 700; letter-spacing: 1px; color: #0d6efd !important; }
        
        /* 卡片美化 */
        .card { background-color: var(--card-bg); border: 1px solid #333; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.2); margin-bottom: 24px; transition: transform 0.2s; }
        .card-header { background-color: rgba(255,255,255,0.03); border-bottom: 1px solid #333; padding: 15px 20px; font-weight: 600; border-radius: 12px 12px 0 0 !important; }
        
        /* 帮助文本 */
        .form-text { color: #888; font-size: 0.85em; margin-top: 5px; }
        .help-tip { background: rgba(13, 110, 253, 0.1); color: #5aa9ff; padding: 10px; border-radius: 6px; border-left: 3px solid #0d6efd; font-size: 0.9em; margin-bottom: 15px; }
        
        /* 日志窗口 */
        .log-box { background: #000; color: #4af626; font-family: 'JetBrains Mono', monospace; height: 500px; overflow-y: auto; padding: 15px; border-radius: 8px; border: 1px solid #333; font-size: 13px; }
        
        /* 移动端适配 */
        @media (max-width: 768px) {
            .navbar-collapse { background: var(--card-bg); padding: 15px; border-radius: 0 0 12px 12px; border-top: 1px solid #333; margin-top: 10px; }
            .log-box { height: 350px; }
            .card-body { padding: 15px; }
        }
        
        /* 动画 */
        .fade-in { animation: fadeIn 0.5s ease-in-out; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
    </style>
</head>
<body class="fade-in">
<nav class="navbar navbar-expand-lg navbar-dark fixed-top">
  <div class="container">
    <a class="navbar-brand" href="/"><i class="fa-solid fa-cloud-arrow-up me-2"></i>Rclone Pro</a>
    <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#nav">
        <span class="navbar-toggler-icon"></span>
    </button>
    <div class="collapse navbar-collapse" id="nav">
      <ul class="navbar-nav ms-auto gap-2">
        <li class="nav-item"><a class="nav-link" href="/"><i class="fa-solid fa-gauge me-1"></i>仪表盘</a></li>
        <li class="nav-item"><a class="nav-link" href="/history"><i class="fa-solid fa-clock-rotate-left me-1"></i>清单</a></li>
        <li class="nav-item"><a class="nav-link" href="/settings"><i class="fa-solid fa-sliders me-1"></i>配置中心</a></li>
        <li class="nav-item"><a class="nav-link" href="/wizard"><i class="fa-solid fa-wand-magic-sparkles me-1"></i>新建向导</a></li>
        <li class="nav-item"><a class="nav-link" href="/help"><i class="fa-solid fa-circle-question me-1"></i>帮助文档</a></li>
        <li class="nav-item"><a class="nav-link text-danger" href="/logout"><i class="fa-solid fa-right-from-bracket"></i></a></li>
      </ul>
    </div>
  </div>
</nav>

<div class="container py-4">
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        {% for cat, msg in messages %}
          <div class="alert alert-{{ cat }} alert-dismissible fade show shadow-sm">
            <i class="fa-solid fa-bell me-2"></i>{{ msg }}
            <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
          </div>
        {% endfor %}
      {% endif %}
    {% endwith %}
"""

HTML_FOOTER = """
</div>
<footer class="text-center text-muted py-4 small">
    <p>Rclone Auto Web Pro v4.0 &copy; 2026 | Powered by Flask & Docker</p>
</footer>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
<script>
    const lb = document.querySelector('.log-box'); 
    if(lb) lb.scrollTop = lb.scrollHeight;
    
    // 自动填充路径提示
    function updateTip(select) {
        const tips = {
            'webdav': '通常 http://IP:端口/dav',
            'smb': '通常 //IP/ShareName',
            'ftp': '通常 IP:21',
            'sftp': '通常 IP:22'
        };
        const type = select.value;
        const tipDiv = document.getElementById('url-tip');
        if(tipDiv) tipDiv.innerText = tips[type] || '请输入服务器地址';
    }
</script>
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
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>body{height:100vh;display:flex;align-items:center;justify-content:center;background:#121212}</style></head>
    <body><div class="card p-5 shadow-lg border-0" style="width:380px;background:#1e1e1e">
    <div class="text-center mb-4"><h2 class="fw-bold text-primary">Rclone Pro</h2><p class="text-muted">全能自动上传面板</p></div>
    <form method="post"><div class="mb-4"><input type="password" name="password" class="form-control form-control-lg bg-dark text-white border-secondary" placeholder="请输入访问密码" required></div>
    <button class="btn btn-primary w-100 btn-lg">安全登录</button></form></div></body></html>
    """)

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

@app.route('/')
@login_required
def dashboard():
    logs = "加载中..."
    if os.path.exists(RCLONE_LOG_FILE):
        try: with open(RCLONE_LOG_FILE, 'r') as f: logs = f.read()[-8000:]
        except: pass
    s = load_settings()
    content = """
    <div class="row g-4">
        <div class="col-lg-4">
            <div class="card h-100">
                <div class="card-header"><i class="fa-solid fa-server me-2"></i>运行概览</div>
                <div class="card-body">
                    <ul class="list-group list-group-flush mb-4 bg-transparent">
                        <li class="list-group-item bg-transparent text-white d-flex justify-content-between px-0">
                            <span>系统状态</span><span class="badge bg-success">运行中</span>
                        </li>
                        <li class="list-group-item bg-transparent text-white d-flex justify-content-between px-0">
                            <span>远程仓库</span><span class="text-info font-monospace">{{ s['rclone_remote'] or '未配置' }}</span>
                        </li>
                         <li class="list-group-item bg-transparent text-white d-flex justify-content-between px-0">
                            <span>上传路径</span><span class="text-warning font-monospace">{{ s['rclone_path'] }}</span>
                        </li>
                    </ul>
                    <div class="d-grid gap-2">
                        {% if not s['rclone_remote'] %}
                        <a href="/wizard" class="btn btn-primary pulse"><i class="fa-solid fa-plus me-2"></i>去创建连接</a>
                        {% else %}
                        <a href="/settings" class="btn btn-outline-light"><i class="fa-solid fa-sliders me-2"></i>修改配置</a>
                        {% endif %}
                    </div>
                </div>
            </div>
        </div>
        <div class="col-lg-8">
            <div class="card h-100">
                <div class="card-header d-flex justify-content-between align-items-center">
                    <span><i class="fa-solid fa-terminal me-2"></i>实时传输日志</span>
                    <a href="/" class="btn btn-sm btn-outline-secondary"><i class="fa-solid fa-rotate"></i></a>
                </div>
                <div class="card-body p-0">
                    <div class="log-box border-0 rounded-0 rounded-bottom">{{ logs }}</div>
                </div>
            </div>
        </div>
    </div>
    <style>.pulse{animation: pulse 2s infinite;} @keyframes pulse {0%{box-shadow: 0 0 0 0 rgba(13, 110, 253, 0.7);} 70%{box-shadow: 0 0 0 10px rgba(13, 110, 253, 0);} 100%{box-shadow: 0 0 0 0 rgba(13, 110, 253, 0);}}</style>
    """
    return render_template_string(HTML_HEADER + content + HTML_FOOTER, logs=logs, s=s)

@app.route('/wizard', methods=['GET', 'POST'])
@login_required
def wizard():
    if request.method == 'POST':
        # 自动生成 rclone config
        type = request.form.get('type')
        name = request.form.get('name')
        url_addr = request.form.get('url')
        user = request.form.get('user')
        password = request.form.get('pass')
        
        # 简单的 obfuscate (rclone 需要 obscurity 密码，这里简化直接存明文或需手动处理，
        # 为了兼容性，这里我们直接生成 WebDAV/FTP 标准格式，Rclone 可以读取明文配置如果版本支持，
        # 或者提示用户这只是简易生成器)
        # *更好的做法是写入文件后让 rclone 自己处理，但这里我们用简单的追加模式*
        
        config_block = f"\n[{name}]\ntype = {type}\n"
        if type == 'webdav':
            config_block += f"url = {url_addr}\nvendor = other\nuser = {user}\npass = {subprocess.check_output(['rclone', 'obscure', password]).decode().strip()}\n"
        elif type == 'ftp':
            host, port = url_addr.split(':') if ':' in url_addr else (url_addr, '21')
            config_block += f"host = {host}\nport = {port}\nuser = {user}\npass = {subprocess.check_output(['rclone', 'obscure', password]).decode().strip()}\n"
        elif type == 'smb':
            config_block += f"host = {url_addr}\nuser = {user}\npass = {subprocess.check_output(['rclone', 'obscure', password]).decode().strip()}\n"
        
        try:
            with open(RCLONE_CONF, 'a') as f:
                f.write(config_block)
            
            # 更新 settings
            s = load_settings()
            s['rclone_remote'] = f"{name}:"
            save_settings(s)
            
            flash(f'成功添加存储 [{name}] 并已设为默认！', 'success')
            return redirect(url_for('dashboard'))
        except Exception as e:
            flash(f'添加失败: {e}', 'danger')

    content = """
    <div class="row justify-content-center"><div class="col-md-8">
        <div class="card">
            <div class="card-header bg-primary text-white"><i class="fa-solid fa-wand-magic-sparkles me-2"></i>新建存储连接向导</div>
            <div class="card-body p-4">
                <div class="help-tip">
                    <i class="fa-solid fa-lightbulb me-2"></i>提示：此向导支持最常用的协议。如果需要添加 <b>百度网盘/OneDrive/GoogleDrive</b> 等需网页授权的存储，请使用电脑端的 Rclone 配置好后，复制内容到 <a href="/edit_conf">配置编辑</a> 页面。
                </div>
                <form method="post">
                    <div class="mb-3">
                        <label class="form-label fw-bold">存储类型</label>
                        <select name="type" class="form-select form-select-lg" onchange="updateTip(this)" required>
                            <option value="webdav">WebDAV (Alist / 123云盘 / 坚果云)</option>
                            <option value="smb">SMB (Windows 共享 / NAS)</option>
                            <option value="ftp">FTP / SFTP</option>
                        </select>
                    </div>
                    <div class="mb-3">
                        <label class="form-label">连接名称 (自定义)</label>
                        <input type="text" name="name" class="form-control" placeholder="例如: my_alist" required pattern="[a-zA-Z0-9_]+">
                        <div class="form-text">只能包含字母、数字和下划线</div>
                    </div>
                    <div class="mb-3">
                        <label class="form-label">服务器地址</label>
                        <input type="text" name="url" class="form-control" placeholder="http://..." required>
                        <div class="form-text text-info" id="url-tip">通常 http://IP:端口/dav</div>
                    </div>
                    <div class="row">
                        <div class="col-md-6 mb-3">
                            <label class="form-label">账号</label>
                            <input type="text" name="user" class="form-control" placeholder="Username" required>
                        </div>
                        <div class="col-md-6 mb-3">
                            <label class="form-label">密码</label>
                            <input type="password" name="pass" class="form-control" placeholder="Password" required>
                        </div>
                    </div>
                    <button type="submit" class="btn btn-primary w-100 btn-lg mt-3">立即添加并使用</button>
                </form>
            </div>
        </div>
    </div></div>
    """
    return render_template_string(HTML_HEADER + content + HTML_FOOTER)

@app.route('/help')
@login_required
def help_page():
    content = """
    <div class="row justify-content-center"><div class="col-lg-10">
        <h3 class="mb-4 text-primary"><i class="fa-solid fa-book-open me-2"></i>帮助中心</h3>
        
        <div class="accordion" id="helpAcc">
            <div class="accordion-item bg-dark border-secondary">
                <h2 class="accordion-header"><button class="accordion-button collapsed bg-dark text-white" type="button" data-bs-toggle="collapse" data-bs-target="#c1">
                    ❓ 如何获取 QQ 邮箱授权码 (SMTP)?
                </button></h2>
                <div id="c1" class="accordion-collapse collapse" data-bs-parent="#helpAcc">
                    <div class="accordion-body text-secondary">
                        <ol>
                            <li>登录电脑版 QQ 邮箱 (mail.qq.com)</li>
                            <li>点击左上角【设置】 -> 【账号】</li>
                            <li>向下滚动找到【POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV服务】</li>
                            <li>开启【POP3/SMTP服务】，点击【生成授权码】</li>
                            <li>将生成的 16 位字符串填入通知设置的【密码】栏（注意：不是你的 QQ 登录密码！）</li>
                        </ol>
                    </div>
                </div>
            </div>
            
            <div class="accordion-item bg-dark border-secondary">
                <h2 class="accordion-header"><button class="accordion-button collapsed bg-dark text-white" type="button" data-bs-toggle="collapse" data-bs-target="#c2">
                    ❓ 如何挂载 123云盘 / 阿里云盘？
                </button></h2>
                <div id="c2" class="accordion-collapse collapse" data-bs-parent="#helpAcc">
                    <div class="accordion-body text-secondary">
                        <p>推荐使用 <b>Alist</b> 先挂载这些网盘，然后通过 WebDAV 协议连接到本程序。</p>
                        <ul>
                            <li><b>Alist 地址:</b> <code>http://你的AlistIP:5244/dav</code></li>
                            <li><b>账号密码:</b> Alist 的后台账号密码</li>
                            <li>在【新建向导】中选择 <b>WebDAV</b> 类型填入即可。</li>
                        </ul>
                    </div>
                </div>
            </div>

            <div class="accordion-item bg-dark border-secondary">
                <h2 class="accordion-header"><button class="accordion-button collapsed bg-dark text-white" type="button" data-bs-toggle="collapse" data-bs-target="#c3">
                    ❓ 为什么上传完成后文件没被删除？
                </button></h2>
                <div id="c3" class="accordion-collapse collapse" data-bs-parent="#helpAcc">
                    <div class="accordion-body text-secondary">
                        <p>请检查以下几点：</p>
                        <ol>
                            <li>在【配置中心】里是否开启了 <b>自动清理本地</b> 开关。</li>
                            <li>只有 <b>上传成功</b> (日志显示绿色对号) 的文件才会被删除。上传失败的文件会保留以防丢失。</li>
                            <li>如果是 Docker 映射问题，请确保容器有对目录的写入/删除权限（本容器已开启 privileged 模式，通常没问题）。</li>
                        </ol>
                    </div>
                </div>
            </div>
        </div>
        
        <div class="mt-5 text-center">
            <p class="text-muted">更多高级用法，请参考项目 GitHub Readme</p>
        </div>
    </div></div>
    """
    return render_template_string(HTML_HEADER + content + HTML_FOOTER)

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        if 'test_email' in request.form:
            send_notification("Rclone Pro 测试", "🎉 恭喜！邮件通知配置正确。")
            flash('测试邮件已发送', 'info')
            return redirect(url_for('settings'))
        
        # 保存逻辑...
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
    <div class="row justify-content-center"><div class="col-lg-10">
    <form method="post">
        <div class="card mb-4">
            <div class="card-header"><i class="fa-solid fa-sliders me-2"></i>基础设置</div>
            <div class="card-body">
                <div class="row mb-3">
                     <div class="col-md-6"><div class="form-check form-switch p-2 border rounded border-secondary"><input class="form-check-input ms-0 me-2" type="checkbox" name="prevent_reupload" {% if s['prevent_reupload'] %}checked{% endif %}><label>防重复上传 (推荐开启)</label></div></div>
                     <div class="col-md-6"><div class="form-check form-switch p-2 border rounded border-secondary"><input class="form-check-input ms-0 me-2" type="checkbox" name="auto_delete" {% if s['auto_delete'] %}checked{% endif %}><label>自动清理本地 (上传后删除)</label></div></div>
                </div>
                <div class="mb-3"><label class="form-label">远程仓库 (Remote)</label><div class="input-group"><select name="rclone_remote" class="form-select bg-dark text-white"><option value="">-- 请选择 --</option>{% for r in remotes %}<option value="{{ r }}" {% if s['rclone_remote'] == r %}selected{% endif %}>{{ r }}</option>{% endfor %}</select></div><div class="form-text">没有选项？请先去 <a href="/wizard">新建向导</a> 创建。</div></div>
                <div class="mb-3"><label class="form-label">上传路径</label><input type="text" name="rclone_path" class="form-control bg-dark text-white font-monospace" value="{{ s['rclone_path'] }}"><div class="form-text">远程文件夹路径，例如 /Movie</div></div>
            </div>
        </div>
        
        <div class="card mb-4">
            <div class="card-header"><i class="fa-solid fa-bell me-2"></i>通知设置</div>
            <div class="card-body">
                <h6 class="text-info mb-3">📧 邮件通知 (SMTP)</h6>
                <div class="form-check form-switch mb-2"><input class="form-check-input" type="checkbox" name="notify_email_enable" {% if s['notify_email_enable'] %}checked{% endif %}><label>启用邮件通知</label></div>
                <div class="row g-2 mb-2">
                    <div class="col-md-8"><input type="text" name="smtp_server" class="form-control form-control-sm bg-dark text-white" placeholder="SMTP服务器 (smtp.qq.com)" value="{{ s['smtp_server'] }}"></div>
                    <div class="col-md-4"><input type="text" name="smtp_port" class="form-control form-control-sm bg-dark text-white" placeholder="端口 (465)" value="{{ s['smtp_port'] }}"></div>
                </div>
                <div class="row g-2 mb-2">
                    <div class="col-md-6"><input type="text" name="smtp_user" class="form-control form-control-sm bg-dark text-white" placeholder="发件邮箱账号" value="{{ s['smtp_user'] }}"></div>
                    <div class="col-md-6">
                        <input type="password" name="smtp_pass" class="form-control form-control-sm bg-dark text-white" placeholder="邮箱授权码/密码" value="{{ s['smtp_pass'] }}">
                        <div class="form-text mt-0">注意：QQ邮箱请填“授权码”，非登录密码。</div>
                    </div>
                </div>
                <div class="input-group input-group-sm mb-3">
                    <span class="input-group-text bg-secondary border-secondary text-white">收件人</span>
                    <input type="text" name="email_to" class="form-control bg-dark text-white" value="{{ s['email_to'] }}">
                    <button type="submit" name="test_email" value="1" class="btn btn-info">测试</button>
                </div>
            </div>
        </div>
        
        <div class="d-grid pb-5"><button class="btn btn-primary btn-lg">保存所有更改</button></div>
    </form></div></div>
    """
    return render_template_string(HTML_HEADER + content + HTML_FOOTER, s=s, remotes=remotes)

@app.route('/history')
@login_required
def history():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM history ORDER BY id DESC LIMIT 100").fetchall()
    conn.close()
    content = """
    <div class="card"><div class="card-header d-flex justify-content-between"><span>历史记录</span><form action="/clear_history" method="post" onsubmit="return confirm('清空？')"><button class="btn btn-sm btn-danger">清空</button></form></div>
    <div class="table-responsive"><table class="table table-dark table-striped mb-0 small"><thead><tr><th>文件</th><th>大小</th><th>时间</th><th>状态</th></tr></thead><tbody>
    {% for r in rows %}<tr><td>{{ r['filename'] }}</td><td>{{ (r['size']/1024/1024)|round(2) }}M</td><td>{{ r['upload_time'] }}</td><td>{{ r['status'] }}</td></tr>{% endfor %}
    </tbody></table></div></div>
    """
    return render_template_string(HTML_HEADER + content + HTML_FOOTER, rows=rows)

@app.route('/edit_conf', methods=['GET', 'POST'])
@login_required
def edit_conf():
    if request.method == 'POST':
        with open(RCLONE_CONF, 'w') as f: f.write(request.form.get('content'))
        flash('已保存', 'success')
        return redirect(url_for('edit_conf'))
    c = ""
    if os.path.exists(RCLONE_CONF):
        with open(RCLONE_CONF, 'r') as f: c = f.read()
    content = """
    <div class="card h-100"><div class="card-header d-flex justify-content-between"><span>rclone.conf</span><button type="submit" form="f1" class="btn btn-sm btn-success">保存</button></div>
    <div class="card-body p-0"><form id="f1" method="post"><textarea name="content" class="form-control bg-dark text-white font-monospace border-0" style="height:500px" spellcheck="false">""" + c + """</textarea></form></div></div>
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

if __name__ == "__main__":
    try:
        init_db()
        start_watcher()
        port = int(os.getenv('PANEL_PORT', 5572))
        print(f"✅ 面板启动: http://0.0.0.0:{port}")
        app.run(host='0.0.0.0', port=port)
    except Exception as e:
        traceback.print_exc()
        while True: time.sleep(100)
