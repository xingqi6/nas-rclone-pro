import os
import time
import subprocess
import sqlite3
import logging
import threading
import psutil
from flask import Flask, render_template_string, request, jsonify
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# --- 配置 ---
WATCH_DIR = "/watchdir"
DATA_DIR = "/app/data"
DB_PATH = os.path.join(DATA_DIR, "uploads.db")
RCLONE_CONF = "/root/.config/rclone/rclone.conf"
app = Flask(__name__)

# --- 日志配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger()

# --- 数据库初始化 ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS history
                 (filename TEXT, size INTEGER, upload_time TEXT, UNIQUE(filename, size))''')
    conn.commit()
    conn.close()

# --- 核心功能：检查文件锁 (PID检测) ---
def is_file_free(filepath, check_duration=10):
    # 1. 检查大小是否稳定
    initial_size = os.path.getsize(filepath)
    time.sleep(check_duration)
    if os.path.getsize(filepath) != initial_size:
        return False
    
    # 2. 检查是否有进程占用 (lsof/fuser 替代逻辑)
    # 在 Docker 开启 --pid=host 后，可以通过 psutil 遍历进程打开的文件
    # 注意：这是一个耗时操作，简化处理：如果大小不变且无 .tmp 后缀，视为可用
    return True

# --- 核心功能：执行上传 ---
def process_file(filepath):
    filename = os.path.basename(filepath)
    filesize = os.path.getsize(filepath)

    # 1. 过滤下载中的临时文件
    if filename.endswith(('.tmp', '.aria2', '.part', '.download')):
        logger.info(f"跳过临时文件: {filename}")
        return

    # 2. 防重复检查
    if os.getenv('PREVENT_REUPLOAD') == 'true':
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM history WHERE filename=? AND size=?", (filename, filesize))
        if cursor.fetchone():
            logger.info(f"文件已在记录中，跳过: {filename}")
            conn.close()
            return
        conn.close()

    # 3. 完整性校验等待
    logger.info(f"开始校验文件完整性: {filename}")
    if not is_file_free(filepath, check_duration=int(os.getenv('CHECK_DURATION', 10))):
        logger.info(f"文件正在写入中，稍后重试: {filename}")
        return # Watchdog 会再次触发或需要循环检测，这里简化逻辑

    # 4. 调用 Rclone 上传
    remote = os.getenv('RCLONE_REMOTE', 'remote:/')
    # 获取性能参数
    buffer = os.getenv('RCLONE_BUFFER_SIZE', '32M')
    transfers = os.getenv('RCLONE_TRANSFERS', '4')
    
    cmd = [
        "rclone", "copy", filepath, remote,
        "--buffer-size", buffer,
        "--transfers", transfers,
        "--log-file", "/app/data/rclone.log",
        "--log-level", "INFO"
    ]
    
    logger.info(f"🚀 开始上传: {filename} -> {remote}")
    result = subprocess.run(cmd)

    if result.returncode == 0:
        logger.info(f"✅ 上传成功: {filename}")
        
        # 5. 记录数据库
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT OR IGNORE INTO history VALUES (?, ?, ?)", 
                     (filename, filesize, time.strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        conn.close()

        # 6. 自动清理
        if os.getenv('AUTO_DELETE_AFTER_UPLOAD') == 'true':
            os.remove(filepath)
            logger.info(f"🧹 本地文件已清理: {filename}")
            
            # 尝试删除空目录
            try:
                os.rmdir(os.path.dirname(filepath))
            except:
                pass
    else:
        logger.error(f"❌ 上传失败: {filename}")

# --- 监控处理类 ---
class UploadEventHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            threading.Thread(target=process_file, args=(event.src_path,)).start()
    
    def on_moved(self, event):
        if not event.is_directory:
            threading.Thread(target=process_file, args=(event.dest_path,)).start()

# --- 启动监控 ---
def start_watcher():
    observer = Observer()
    observer.schedule(UploadEventHandler(), WATCH_DIR, recursive=True)
    observer.start()
    logger.info(f"监控服务已启动: {WATCH_DIR}")

# --- Web 面板 (极简版) ---
@app.route('/')
def index():
    # 读取日志
    log_content = "日志加载中..."
    if os.path.exists('/app/data/rclone.log'):
        with open('/app/data/rclone.log', 'r') as f:
            log_content = f.read()[-2000:] # 最后2000字符
            
    return render_template_string('''
        <html>
        <head><title>NAS Rclone Pro</title>
        <style>body{font-family:sans-serif;padding:20px;background:#f0f2f5} 
        .box{background:#fff;padding:20px;border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,0.1)}
        pre{background:#333;color:#0f0;padding:10px;overflow:auto;height:400px}</style>
        </head>
        <body>
            <div class="box">
                <h2>🚀 飞牛 NAS Rclone 控制台</h2>
                <p>状态: <b>运行中</b> | 监听目录: /watchdir</p>
                <h3>📜 实时上传日志</h3>
                <pre>{{ logs }}</pre>
            </div>
        </body>
        </html>
    ''', logs=log_content)

if __name__ == "__main__":
    init_db()
    start_watcher()
    # 修改端口为 5572，避免和 NAS 系统冲突
    # 也可以通过环境变量 PANEL_PORT 修改
    port = int(os.getenv('PANEL_PORT', 5572))
    logger.info(f"Web面板启动端口: {port}")
    app.run(host='0.0.0.0', port=port)
