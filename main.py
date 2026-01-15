import os
import time
import subprocess
import sqlite3
import logging
import threading
import json
import psutil
from functools import wraps
from flask import Flask, render_template_string, request, redirect, url_for, session, flash, jsonify
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
app.secret_key = os.urandom(24)  # 用于 session 加密

# --- 日志配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger()

# --- 默认设置 (如果 settings.json 不存在) ---
DEFAULT_SETTINGS = {
    "check_duration": 10,       # 文件稳定校验时长(秒)
    "prevent_reupload": True,   # 防重复上传
    "auto_delete": True,        # 上传后自动清理
    "rclone_buffer": "64M",     # 缓冲区大小
    "rclone_transfers": "4",    # 并发数
    "rclone_checkers": "8",     # 检查器数
    "notify_enabled": False     # 通知开关(预留)
}

# --- 工具函数 ---
def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r') as f:
                return json.load(f)
        except:
            return DEFAULT_SETTINGS
    return DEFAULT_SETTINGS

def save_settings(new_settings):
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(new_settings, f, indent=4)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS history
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  filename TEXT, 
                  size INTEGER, 
                  upload_time TEXT, 
                  status TEXT,
                  UNIQUE(filename, size))''')
    conn.commit()
    conn.close()

# --- 登录验证装饰器 ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# --- 核心逻辑: 文件检测与上传 ---
def is_file_free(filepath, duration):
    try:
        size1 = os.path.getsize(filepath)
        time.sleep(duration)
        size2 = os.path.getsize(filepath)
        return size1 == size2
    except:
        return False

def process_file(filepath):
    if not os.path.exists(filepath): return
    filename = os.path.basename(filepath)
    
    # 过滤临时文件
    if filename.endswith(('.tmp', '.aria2', '.part', '.downloading', '.ds_store')):
        return

    settings = load_settings()
    filesize = os.path.getsize(filepath)

    # 1. 防重复检查
    if settings['prevent_reupload']:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM history WHERE filename=? AND size=? AND status='success'", (filename, filesize))
        if cursor.fetchone():
            logger.info(f"🚫 [防重] 跳过已上传文件: {filename}")
            conn.close()
            # 如果开启清理，且文件已存在于历史中，直接清理本地
            if settings['auto_delete']:
                try:
                    os.remove(filepath)
                    logger.info(f"🧹 [清理] 删除重复的本地副本: {filename}")
                except: pass
            return
        conn.close()

    # 2. 完整性校验
    logger.info(f"🔍 [校验] 正在检测文件: {filename}")
    if not is_file_free(filepath, int(settings['check_duration'])):
        logger.info(f"⏳ [等待] 文件正在写入: {filename}")
        return # 等待下次触发或由 watchdog 处理

    # 3. Rclone 上传
    remote = os.getenv('RCLONE_REMOTE', 'remote:/') # 依然优先读取环境变量，也可改为从UI配置
    
    cmd = [
        "rclone", "copy", filepath, remote,
        "--buffer-size", str(settings['rclone_buffer']),
        "--transfers", str(settings['rclone_transfers']),
        "--checkers", str(settings['rclone_checkers']),
        "--log-file", RCLONE_LOG_FILE,
        "--log-level", "INFO"
    ]

    logger.info(f"🚀 [上传] 开始传输: {filename}")
    try:
        result = subprocess.run(cmd)
        status = "success" if result.returncode == 0 else "failed"
        
        # 记录数据库
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT OR REPLACE INTO history (filename, size, upload_time, status) VALUES (?, ?, ?, ?)", 
                     (filename, filesize, time.strftime('%Y-%m-%d %H:%M:%S'), status))
        conn.commit()
        conn.close()

        if status == "success":
            logger.info(f"✅ [成功] 上传完成: {filename}")
            if settings['auto_delete']:
                os.remove(filepath)
                logger.info(f"🧹 [清理] 本地文件已删除: {filename}")
                try:
                    parent = os.path.dirname(filepath)
                    if not os.listdir(parent) and parent != WATCH_DIR:
                        os.rmdir(parent)
                except: pass
        else:
            logger.error(f"❌ [失败] 上传出错: {filename}")

    except Exception as e:
        logger.error(f"❌ [异常] {str(e)}")

# --- 监控线程 ---
class Handler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory: threading.Thread(target=process_file, args=(event.src_path,)).start()
    def on_moved(self, event):
        if not event.is_directory: threading.Thread(target=process_file, args=(event.dest_path,)).start()

def start_watcher():
    observer = Observer()
    observer.schedule(Handler(), WATCH_DIR, recursive=True)
    observer.start()

# --- 前端模板 (HTML/CSS/JS) ---
# 为了方便单文件部署，直接嵌入 HTML
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>飞牛 NAS Rclone 面板</title>
    <link href="https://cdn.bootcdn.net/ajax/libs/twitter-bootstrap/5.3.0/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.bootcdn.net/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <style>
        :root { --bs-body-bg: #121212; --bs-body-color: #e0e0e0; --card-bg: #1e1e1e; }
        body { background-color: var(--bs-body-bg); color: var(--bs-body-color); font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }
        .navbar { background-color: #2c2c2c !important; border-bottom: 1px solid #444; }
        .card { background-color: var(--card-bg); border: 1px solid #333; margin-bottom: 20px; }
        .card-header { background-color: #252525; border-bottom: 1px solid #333; font-weight: bold; }
        .log-box { background: #000; color: #00ff00; font-family: monospace; height: 500px; overflow-y: scroll; padding: 15px; border-radius: 5px; border: 1px solid #444; }
        .nav-link.active { background-color: #0d6efd !important; color: white !important; }
        .table { color: #ccc; }
        .form-control, .form-select { background-color: #2b2b2b; border: 1px solid #444; color: #fff; }
        .form-control:focus { background-color: #2b2b2b; color: #fff; border-color: #0d6efd; }
        .btn-primary { background-color: #0d6efd; border: none; }
        /* Toggle Switch */
        .form-check-input { width: 3em; height: 1.5em; cursor: pointer; }
    </style>
</head>
<body>

{% if session.logged_in %}
<nav class="navbar navbar-expand-lg navbar-dark mb-4">
  <div class="container">
    <a class="navbar-brand" href="/"><i class="fa-solid fa-rocket me-2"></i>飞牛 Rclone Pro</a>
    <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#navbarNav">
      <span class="navbar-toggler-icon"></span>
    </button>
    <div class="collapse navbar-collapse" id="navbarNav">
      <ul class="navbar-nav ms-auto">
        <li class="nav-item"><a class="nav-link {{ 'active' if page=='dashboard' }}" href="/"><i class="fa-solid fa-gauge me-1"></i>仪表盘</a></li>
        <li class="nav-item"><a class="nav-link {{ 'active' if page=='history' }}" href="/history"><i class="fa-solid fa-list-check me-1"></i>上传清单</a></li>
        <li class="nav-item"><a class="nav-link {{ 'active' if page=='settings' }}" href="/settings"><i class="fa-solid fa-sliders me-1"></i>高级配置</a></li>
        <li class="nav-item"><a class="nav-link {{ 'active' if page=='rclone' }}" href="/rclone"><i class="fa-solid fa-cloud me-1"></i>Rclone管理</a></li>
        <li class="nav-item"><a class="nav-link text-danger" href="/logout"><i class="fa-solid fa-power-off"></i></a></li>
      </ul>
    </div>
  </div>
</nav>
{% endif %}

<div class="container">
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        {% for category, message in messages %}
          <div class="alert alert-{{ category }} alert-dismissible fade show" role="alert">
            {{ message }}
            <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
          </div>
        {% endfor %}
      {% endif %}
    {% endwith %}

    {% block content %}{% endblock %}
</div>

<script src="https://cdn.bootcdn.net/ajax/libs/bootstrap/5.3.0/js/bootstrap.bundle.min.js"></script>
<script>
    // 自动滚动日志到底部
    const logBox = document.querySelector('.log-box');
    if(logBox) logBox.scrollTop = logBox.scrollHeight;
</script>
</body>
</html>
"""

# --- 页面路由 ---

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        # 获取环境变量中的密码，默认 123456
        sys_pass = os.getenv('PANEL_PASSWORD', '123456')
        if request.form['password'] == sys_pass:
            session['logged_in'] = True
            return redirect(url_for('dashboard'))
        else:
            flash('密码错误', 'danger')
    
    return render_template_string("""
    <!DOCTYPE html>
    <html lang="zh">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>登录 - Rclone Panel</title>
        <link href="https://cdn.bootcdn.net/ajax/libs/twitter-bootstrap/5.3.0/css/bootstrap.min.css" rel="stylesheet">
        <style>
            body { background: #121212; color: #fff; height: 100vh; display: flex; align-items: center; justify-content: center; }
            .login-box { background: #1e1e1e; padding: 40px; border-radius: 10px; border: 1px solid #333; width: 100%; max-width: 400px; }
            .btn-primary { width: 100%; }
        </style>
    </head>
    <body>
        <div class="login-box">
            <h3 class="text-center mb-4">🚀 Rclone Panel</h3>
            <form method="post">
                <div class="mb-3">
                    <input type="password" name="password" class="form-control" placeholder="输入面板密码 (默认123456)" required>
                </div>
                <button type="submit" class="btn btn-primary">登录</button>
            </form>
        </div>
    </body>
    </html>
    """)

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

@app.route('/')
@login_required
def dashboard():
    logs = "暂无日志..."
    if os.path.exists(RCLONE_LOG_FILE):
        try:
            with open(RCLONE_LOG_FILE, 'r') as f:
                logs = f.read()[-5000:]
        except: pass
    
    return render_template_string(HTML_TEMPLATE + """
    {% block content %}
    <div class="row">
        <div class="col-md-4">
            <div class="card">
                <div class="card-header"><i class="fa-solid fa-circle-info me-2"></i>运行状态</div>
                <div class="card-body">
                    <p>状态: <span class="badge bg-success">运行中 ●</span></p>
                    <p>端口: <span class="text-info">{{ port }}</span></p>
                    <p>监听目录: <code class="text-warning">/watchdir</code></p>
                    <p>远程仓库: <code class="text-info">{{ remote }}</code></p>
                </div>
            </div>
             <div class="card">
                <div class="card-header"><i class="fa-solid fa-bolt me-2"></i>快捷操作</div>
                <div class="card-body">
                    <a href="/settings" class="btn btn-outline-primary w-100 mb-2">修改配置</a>
                    <a href="/history" class="btn btn-outline-secondary w-100">查看清单</a>
                </div>
            </div>
        </div>
        <div class="col-md-8">
            <div class="card">
                <div class="card-header d-flex justify-content-between align-items-center">
                    <span><i class="fa-solid fa-terminal me-2"></i>实时传输日志</span>
                    <a href="/" class="btn btn-sm btn-dark"><i class="fa-solid fa-rotate-right"></i> 刷新</a>
                </div>
                <div class="card-body p-0">
                    <div class="log-box">{{ logs }}</div>
                </div>
            </div>
        </div>
    </div>
    {% endblock %}
    """, page='dashboard', logs=logs, port=request.host.split(':')[-1], remote=os.getenv('RCLONE_REMOTE'))

@app.route('/history')
@login_required
def history():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM history ORDER BY id DESC LIMIT 100").fetchall()
    conn.close()
    
    return render_template_string(HTML_TEMPLATE + """
    {% block content %}
    <div class="card">
        <div class="card-header d-flex justify-content-between align-items-center">
            <span><i class="fa-solid fa-clock-rotate-left me-2"></i>最近 100 条上传记录</span>
            <form action="/clear_history" method="post" onsubmit="return confirm('确定清空所有记录？这会导致文件被重新上传！');">
                <button type="submit" class="btn btn-sm btn-danger"><i class="fa-solid fa-trash"></i> 清空记录</button>
            </form>
        </div>
        <div class="card-body table-responsive">
            <table class="table table-dark table-hover table-striped">
                <thead><tr><th>ID</th><th>文件名</th><th>大小</th><th>时间</th><th>状态</th></tr></thead>
                <tbody>
                {% for row in rows %}
                <tr>
                    <td>{{ row['id'] }}</td>
                    <td>{{ row['filename'] }}</td>
                    <td>{{ (row['size'] / 1024 / 1024)|round(2) }} MB</td>
                    <td>{{ row['upload_time'] }}</td>
                    <td>
                        {% if row['status'] == 'success' %}
                            <span class="badge bg-success">成功</span>
                        {% else %}
                            <span class="badge bg-danger">失败</span>
                        {% endif %}
                    </td>
                </tr>
                {% endfor %}
                </tbody>
            </table>
        </div>
    </div>
    {% endblock %}
    """, page='history', rows=rows)

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        new_settings = {
            "check_duration": int(request.form.get('check_duration', 10)),
            "prevent_reupload": 'prevent_reupload' in request.form,
            "auto_delete": 'auto_delete' in request.form,
            "rclone_buffer": request.form.get('rclone_buffer', '32M'),
            "rclone_transfers": request.form.get('rclone_transfers', '4'),
            "rclone_checkers": request.form.get('rclone_checkers', '8'),
        }
        save_settings(new_settings)
        flash('配置已保存，即时生效！', 'success')
        return redirect(url_for('settings'))
    
    settings = load_settings()
    return render_template_string(HTML_TEMPLATE + """
    {% block content %}
    <div class="row justify-content-center">
        <div class="col-md-8">
            <div class="card">
                <div class="card-header"><i class="fa-solid fa-sliders me-2"></i>高级配置</div>
                <div class="card-body">
                    <form method="post">
                        <h5 class="text-primary mb-3">🛠️ 核心功能开关</h5>
                        <div class="form-check form-switch mb-3">
                            <input class="form-check-input" type="checkbox" id="prevent_reupload" name="prevent_reupload" {% if settings['prevent_reupload'] %}checked{% endif %}>
                            <label class="form-check-label" for="prevent_reupload">
                                <strong>防重复上传</strong> <br>
                                <small class="text-muted">检测数据库，重启/断网后不重复上传已完成的文件。</small>
                            </label>
                        </div>
                        <div class="form-check form-switch mb-3">
                            <input class="form-check-input" type="checkbox" id="auto_delete" name="auto_delete" {% if settings['auto_delete'] %}checked{% endif %}>
                            <label class="form-check-label" for="auto_delete">
                                <strong>上传后自动清理</strong> <br>
                                <small class="text-muted">上传成功后自动删除本地文件，释放 NAS 空间。</small>
                            </label>
                        </div>
                        
                        <hr class="border-secondary my-4">
                        
                        <h5 class="text-primary mb-3">⚡ 性能与校验</h5>
                        <div class="mb-3">
                            <label class="form-label">文件稳定检测时长 (秒)</label>
                            <input type="number" name="check_duration" class="form-control" value="{{ settings['check_duration'] }}">
                            <div class="form-text text-muted">文件大小保持不变超过此时间才开始上传 (防下载未完成)。</div>
                        </div>
                        <div class="row">
                            <div class="col-md-4 mb-3">
                                <label class="form-label">内存缓冲区 (--buffer-size)</label>
                                <input type="text" name="rclone_buffer" class="form-control" value="{{ settings['rclone_buffer'] }}">
                            </div>
                            <div class="col-md-4 mb-3">
                                <label class="form-label">并发上传数 (--transfers)</label>
                                <input type="number" name="rclone_transfers" class="form-control" value="{{ settings['rclone_transfers'] }}">
                            </div>
                            <div class="col-md-4 mb-3">
                                <label class="form-label">检查器数 (--checkers)</label>
                                <input type="number" name="rclone_checkers" class="form-control" value="{{ settings['rclone_checkers'] }}">
                            </div>
                        </div>
                        
                        <div class="d-grid gap-2 mt-4">
                            <button type="submit" class="btn btn-primary btn-lg"><i class="fa-solid fa-save"></i> 保存配置</button>
                        </div>
                    </form>
                </div>
            </div>
        </div>
    </div>
    {% endblock %}
    """, page='settings', settings=settings)

@app.route('/rclone')
@login_required
def rclone_manage():
    # 读取 remotes
    remotes = []
    try:
        result = subprocess.run(["rclone", "listremotes", "--config", RCLONE_CONF], capture_output=True, text=True)
        remotes = [r.strip() for r in result.stdout.split('\n') if r.strip()]
    except: pass
    
    # 读取 config 内容
    conf_content = ""
    try:
        if os.path.exists(RCLONE_CONF):
            with open(RCLONE_CONF, 'r') as f:
                conf_content = f.read()
    except: pass

    return render_template_string(HTML_TEMPLATE + """
    {% block content %}
    <div class="row">
        <div class="col-md-4">
             <div class="card">
                <div class="card-header">已配置的存储 (Remotes)</div>
                <ul class="list-group list-group-flush">
                    {% for r in remotes %}
                    <li class="list-group-item bg-dark text-white d-flex justify-content-between">
                        <span><i class="fa-solid fa-cloud text-info me-2"></i>{{ r }}</span>
                        <span class="badge bg-primary">可用</span>
                    </li>
                    {% else %}
                    <li class="list-group-item bg-dark text-muted">暂无配置，请挂载配置文件</li>
                    {% endfor %}
                </ul>
            </div>
        </div>
        <div class="col-md-8">
            <div class="card">
                <div class="card-header"><i class="fa-solid fa-file-code me-2"></i>rclone.conf 配置文件内容</div>
                <div class="card-body">
                    <textarea class="form-control bg-dark text-warning font-monospace" rows="15" readonly>{{ conf }}</textarea>
                    <p class="mt-2 text-muted small">注：出于安全考虑，目前仅支持查看。如需修改请挂载宿主机文件。</p>
                </div>
            </div>
        </div>
    </div>
    {% endblock %}
    """, page='rclone', remotes=remotes, conf=conf_content)

@app.route('/clear_history', methods=['POST'])
@login_required
def clear_history():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM history")
    conn.commit()
    conn.close()
    flash('所有历史记录已清空！', 'warning')
    return redirect(url_for('history'))

# --- 启动入口 ---
if __name__ == "__main__":
    init_db()
    start_watcher()
    
    # 端口优先使用环境变量，默认 5572
    port = int(os.getenv('PANEL_PORT', 5572))
    print(f"✅ 全功能 Web 面板启动成功: http://0.0.0.0:{port}")
    app.run(host='0.0.0.0', port=port)
