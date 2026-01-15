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
app.secret_key = os.urandom(24)

# --- 日志配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger()

# --- 默认设置 ---
DEFAULT_SETTINGS = {
    "check_duration": 10,
    "prevent_reupload": True,
    "auto_delete": True,
    "rclone_buffer": "64M",
    "rclone_transfers": "4",
    "rclone_checkers": "8"
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

# --- 核心逻辑 ---
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
    if filename.endswith(('.tmp', '.aria2', '.part', '.downloading', '.ds_store')): return

    settings = load_settings()
    filesize = os.path.getsize(filepath)

    # 1. 防重
    if settings['prevent_reupload']:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM history WHERE filename=? AND size=? AND status='success'", (filename, filesize))
        if cursor.fetchone():
            logger.info(f"🚫 [防重] 跳过: {filename}")
            conn.close()
            if settings['auto_delete']:
                try: os.remove(filepath)
                except: pass
            return
        conn.close()

    # 2. 校验
    logger.info(f"🔍 [校验] 检测文件: {filename}")
    if not is_file_free(filepath, int(settings['check_duration'])):
        logger.info(f"⏳ [等待] 文件正在写入: {filename}")
        return

    # 3. 上传
    remote = os.getenv('RCLONE_REMOTE', 'remote:/')
    cmd = [
        "rclone", "copy", filepath, remote,
        "--buffer-size", str(settings['rclone_buffer']),
        "--transfers", str(settings['rclone_transfers']),
        "--checkers", str(settings['rclone_checkers']),
        "--log-file", RCLONE_LOG_FILE,
        "--log-level", "INFO"
    ]

    logger.info(f"🚀 [上传] 开始: {filename}")
    try:
        result = subprocess.run(cmd)
        status = "success" if result.returncode == 0 else "failed"
        
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT OR REPLACE INTO history (filename, size, upload_time, status) VALUES (?, ?, ?, ?)", 
                     (filename, filesize, time.strftime('%Y-%m-%d %H:%M:%S'), status))
        conn.commit()
        conn.close()

        if status == "success":
            logger.info(f"✅ [完成] {filename}")
            if settings['auto_delete']:
                os.remove(filepath)
                try:
                    parent = os.path.dirname(filepath)
                    if not os.listdir(parent) and parent != WATCH_DIR:
                        os.rmdir(parent)
                except: pass
        else:
            logger.error(f"❌ [失败] {filename}")
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

# --- HTML 模板片段 (修复冲突问题) ---
HTML_HEADER = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>飞牛 NAS Rclone</title>
    <link href="https://cdn.bootcdn.net/ajax/libs/twitter-bootstrap/5.3.0/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.bootcdn.net/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <style>
        :root { --bs-body-bg: #121212; --bs-body-color: #e0e0e0; --card-bg: #1e1e1e; }
        body { background-color: var(--bs-body-bg); color: var(--bs-body-color); font-family: monospace; }
        .navbar { background-color: #2c2c2c !important; border-bottom: 1px solid #444; }
        .card { background-color: var(--card-bg); border: 1px solid #333; margin-bottom: 20px; }
        .card-header { background-color: #252525; border-bottom: 1px solid #333; font-weight: bold; }
        .log-box { background: #000; color: #00ff00; height: 500px; overflow-y: scroll; padding: 15px; border: 1px solid #444; }
        .nav-link.active { background-color: #0d6efd !important; color: white !important; }
        .table { color: #ccc; }
        .form-control, .form-select { background-color: #2b2b2b; border: 1px solid #444; color: #fff; }
        .btn-primary { background-color: #0d6efd; border: none; }
        .form-check-input { width: 3em; height: 1.5em; cursor: pointer; }
    </style>
</head>
<body>
<nav class="navbar navbar-expand-lg navbar-dark mb-4">
  <div class="container">
    <a class="navbar-brand" href="/"><i class="fa-solid fa-rocket me-2"></i>Rclone Pro</a>
    <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#navbarNav"><span class="navbar-toggler-icon"></span></button>
    <div class="collapse navbar-collapse" id="navbarNav">
      <ul class="navbar-nav ms-auto">
        <li class="nav-item"><a class="nav-link" href="/"><i class="fa-solid fa-gauge"></i> 仪表盘</a></li>
        <li class="nav-item"><a class="nav-link" href="/history"><i class="fa-solid fa-list"></i> 清单</a></li>
        <li class="nav-item"><a class="nav-link" href="/settings"><i class="fa-solid fa-sliders"></i> 配置</a></li>
        <li class="nav-item"><a class="nav-link" href="/rclone"><i class="fa-solid fa-cloud"></i> 存储</a></li>
        <li class="nav-item"><a class="nav-link text-danger" href="/logout"><i class="fa-solid fa-power-off"></i></a></li>
      </ul>
    </div>
  </div>
</nav>
<div class="container">
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        {% for category, message in messages %}
          <div class="alert alert-{{ category }} alert-dismissible fade show">{{ message }}<button type="button" class="btn-close" data-bs-dismiss="alert"></button></div>
        {% endfor %}
      {% endif %}
    {% endwith %}
"""

HTML_FOOTER = """
</div>
<script src="https://cdn.bootcdn.net/ajax/libs/bootstrap/5.3.0/js/bootstrap.bundle.min.js"></script>
<script>
    const logBox = document.querySelector('.log-box');
    if(logBox) logBox.scrollTop = logBox.scrollHeight;
</script>
</body>
</html>
"""

# --- 路由 ---

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        sys_pass = os.getenv('PANEL_PASSWORD', '123456')
        if request.form['password'] == sys_pass:
            session['logged_in'] = True
            return redirect(url_for('dashboard'))
        else:
            flash('密码错误', 'danger')
    return render_template_string("""
    <!DOCTYPE html><html lang="zh"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
    <link href="https://cdn.bootcdn.net/ajax/libs/twitter-bootstrap/5.3.0/css/bootstrap.min.css" rel="stylesheet">
    <style>body{background:#121212;color:#fff;height:100vh;display:flex;align-items:center;justify-content:center}.box{background:#1e1e1e;padding:40px;border-radius:10px;border:1px solid #333;width:100%;max-width:400px}</style>
    </head><body><div class="box"><h3 class="text-center mb-4">🚀 Rclone Panel</h3>
    <form method="post"><div class="mb-3"><input type="password" name="password" class="form-control" placeholder="输入密码" required></div>
    <button type="submit" class="btn btn-primary w-100">登录</button></form></div></body></html>
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
    
    content = """
    <div class="row">
        <div class="col-md-4">
            <div class="card">
                <div class="card-header">运行状态</div>
                <div class="card-body">
                    <p>状态: <span class="badge bg-success">运行中</span></p>
                    <p>端口: <span class="text-info">{{ port }}</span></p>
                    <p>远程: <code class="text-warning">{{ remote }}</code></p>
                </div>
            </div>
             <div class="card">
                <div class="card-header">操作</div>
                <div class="card-body">
                    <a href="/settings" class="btn btn-outline-primary w-100 mb-2">修改配置</a>
                    <a href="/history" class="btn btn-outline-secondary w-100">查看清单</a>
                </div>
            </div>
        </div>
        <div class="col-md-8">
            <div class="card">
                <div class="card-header d-flex justify-content-between">
                    <span>实时日志</span>
                    <a href="/" class="btn btn-sm btn-dark">刷新</a>
                </div>
                <div class="card-body p-0">
                    <div class="log-box">{{ logs }}</div>
                </div>
            </div>
        </div>
    </div>
    """
    return render_template_string(HTML_HEADER + content + HTML_FOOTER, logs=logs, port=request.host.split(':')[-1], remote=os.getenv('RCLONE_REMOTE'))

@app.route('/history')
@login_required
def history():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM history ORDER BY id DESC LIMIT 100").fetchall()
    conn.close()
    
    content = """
    <div class="card">
        <div class="card-header d-flex justify-content-between">
            <span>最近 100 条记录</span>
            <form action="/clear_history" method="post" onsubmit="return confirm('确定清空？');">
                <button type="submit" class="btn btn-sm btn-danger">清空记录</button>
            </form>
        </div>
        <div class="table-responsive">
            <table class="table table-dark table-hover mb-0">
                <thead><tr><th>文件</th><th>大小</th><th>时间</th><th>状态</th></tr></thead>
                <tbody>
                {% for row in rows %}
                <tr>
                    <td>{{ row['filename'] }}</td>
                    <td>{{ (row['size']/1024/1024)|round(2) }} MB</td>
                    <td>{{ row['upload_time'] }}</td>
                    <td><span class="badge bg-{{ 'success' if row['status']=='success' else 'danger' }}">{{ row['status'] }}</span></td>
                </tr>
                {% endfor %}
                </tbody>
            </table>
        </div>
    </div>
    """
    return render_template_string(HTML_HEADER + content + HTML_FOOTER, rows=rows)

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
        flash('配置已保存', 'success')
        return redirect(url_for('settings'))
    
    settings = load_settings()
    content = """
    <div class="row justify-content-center"><div class="col-md-8"><div class="card">
    <div class="card-header">高级配置</div><div class="card-body">
    <form method="post">
        <div class="form-check form-switch mb-3">
            <input class="form-check-input" type="checkbox" id="prevent_reupload" name="prevent_reupload" {% if settings['prevent_reupload'] %}checked{% endif %}>
            <label class="form-check-label" for="prevent_reupload">防重复上传</label>
        </div>
        <div class="form-check form-switch mb-3">
            <input class="form-check-input" type="checkbox" id="auto_delete" name="auto_delete" {% if settings['auto_delete'] %}checked{% endif %}>
            <label class="form-check-label" for="auto_delete">上传后自动清理</label>
        </div>
        <hr class="border-secondary">
        <div class="mb-3"><label>检测时长(秒)</label><input type="number" name="check_duration" class="form-control" value="{{ settings['check_duration'] }}"></div>
        <div class="row">
            <div class="col-4"><label>缓冲区</label><input type="text" name="rclone_buffer" class="form-control" value="{{ settings['rclone_buffer'] }}"></div>
            <div class="col-4"><label>并发数</label><input type="number" name="rclone_transfers" class="form-control" value="{{ settings['rclone_transfers'] }}"></div>
            <div class="col-4"><label>检查器</label><input type="number" name="rclone_checkers" class="form-control" value="{{ settings['rclone_checkers'] }}"></div>
        </div>
        <button type="submit" class="btn btn-primary w-100 mt-4">保存</button>
    </form></div></div></div></div>
    """
    return render_template_string(HTML_HEADER + content + HTML_FOOTER, settings=settings)

@app.route('/rclone')
@login_required
def rclone_manage():
    try:
        res = subprocess.run(["rclone", "listremotes", "--config", RCLONE_CONF], capture_output=True, text=True)
        remotes = [r.strip() for r in res.stdout.split('\n') if r.strip()]
    except: remotes = []
    
    conf_content = ""
    if os.path.exists(RCLONE_CONF):
        with open(RCLONE_CONF, 'r') as f: conf_content = f.read()

    content = """
    <div class="row"><div class="col-md-4"><div class="card"><div class="card-header">存储列表</div>
    <ul class="list-group list-group-flush">
        {% for r in remotes %}
        <li class="list-group-item bg-dark text-white">{{ r }} <span class="badge bg-primary float-end">OK</span></li>
        {% else %}
        <li class="list-group-item bg-dark text-muted">暂无配置</li>
        {% endfor %}
    </ul></div></div>
    <div class="col-md-8"><div class="card"><div class="card-header">配置文件内容</div>
    <div class="card-body"><textarea class="form-control bg-dark text-warning" rows="15" readonly>{{ conf }}</textarea></div></div></div></div>
    """
    return render_template_string(HTML_HEADER + content + HTML_FOOTER, remotes=remotes, conf=conf_content)

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
    init_db()
    start_watcher()
    port = int(os.getenv('PANEL_PORT', 5572))
    print(f"✅ 面板已启动: http://0.0.0.0:{port}")
    app.run(host='0.0.0.0', port=port)
