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
# Rclone日志位置，Web面板读取此文件
RCLONE_LOG_FILE = os.path.join(DATA_DIR, "rclone.log") 
app = Flask(__name__)

# --- 日志配置 (打印到控制台，方便 docker logs 查看) ---
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger()

# --- 数据库初始化 ---
def init_db():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS history
                     (filename TEXT, size INTEGER, upload_time TEXT, UNIQUE(filename, size))''')
        conn.commit()
        conn.close()
        logger.info("数据库初始化成功")
    except Exception as e:
        logger.error(f"数据库初始化失败: {e}")

# --- 核心功能：检查文件锁 (PID检测) ---
def is_file_free(filepath, check_duration=10):
    try:
        # 1. 检查大小是否稳定
        initial_size = os.path.getsize(filepath)
        time.sleep(check_duration)
        current_size = os.path.getsize(filepath)
        
        if current_size != initial_size:
            return False
        
        # 2. 简易判断：如果大小没变且没有临时后缀，视为可用
        # (配合 --pid=host 后续可扩展更复杂的 lsof 检测)
        return True
    except FileNotFoundError:
        return False
    except Exception as e:
        logger.error(f"文件检测出错: {e}")
        return False

# --- 核心功能：执行上传 ---
def process_file(filepath):
    if not os.path.exists(filepath):
        return

    filename = os.path.basename(filepath)
    filesize = os.path.getsize(filepath)

    # 1. 过滤下载中的临时文件
    if filename.endswith(('.tmp', '.aria2', '.part', '.download', '.downloading')):
        logger.info(f"⏳ 跳过临时文件: {filename}")
        return

    # 2. 防重复检查
    if os.getenv('PREVENT_REUPLOAD') == 'true':
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM history WHERE filename=? AND size=?", (filename, filesize))
            if cursor.fetchone():
                logger.info(f"🚫 [防重] 文件已在记录中，跳过: {filename}")
                conn.close()
                # 如果开启了自动清理，这里也可以选择清理本地
                if os.getenv('AUTO_DELETE_AFTER_UPLOAD') == 'true':
                    try:
                        os.remove(filepath)
                        logger.info(f"🧹 [清理] 删除已存在的本地副本: {filename}")
                    except:
                        pass
                return
            conn.close()
        except Exception as e:
            logger.error(f"数据库查询失败: {e}")

    # 3. 完整性校验等待
    logger.info(f"🔍 [校验] 正在检查文件完整性: {filename}")
    # 默认检测时长 10秒，可通过环境变量 CHECK_DURATION 修改
    if not is_file_free(filepath, check_duration=int(os.getenv('CHECK_DURATION', 10))):
        logger.info(f"⚠️ [占用] 文件正在写入中或大小在变化，稍后重试: {filename}")
        return 

    # 4. 调用 Rclone 上传
    remote = os.getenv('RCLONE_REMOTE', 'remote:/')
    # 获取性能参数
    buffer = os.getenv('RCLONE_BUFFER_SIZE', '32M')
    transfers = os.getenv('RCLONE_TRANSFERS', '4')
    
    cmd = [
        "rclone", "copy", filepath, remote,
        "--buffer-size", buffer,
        "--transfers", transfers,
        "--log-file", RCLONE_LOG_FILE,
        "--log-level", "INFO"
    ]
    
    logger.info(f"🚀 [上传] 开始传输: {filename} -> {remote}")
    try:
        result = subprocess.run(cmd)
        
        if result.returncode == 0:
            logger.info(f"✅ [成功] 上传完成: {filename}")
            
            # 5. 记录数据库
            conn = sqlite3.connect(DB_PATH)
            conn.execute("INSERT OR IGNORE INTO history VALUES (?, ?, ?)", 
                         (filename, filesize, time.strftime('%Y-%m-%d %H:%M:%S')))
            conn.commit()
            conn.close()

            # 6. 自动清理
            if os.getenv('AUTO_DELETE_AFTER_UPLOAD') == 'true':
                os.remove(filepath)
                logger.info(f"🧹 [清理] 本地文件已删除: {filename}")
                
                # 尝试删除空目录 (递归逻辑由 watchdog 处理，这里只处理父级)
                try:
                    parent_dir = os.path.dirname(filepath)
                    if not os.listdir(parent_dir) and parent_dir != WATCH_DIR:
                        os.rmdir(parent_dir)
                except:
                    pass
        else:
            logger.error(f"❌ [失败] Rclone 退出代码: {result.returncode}")
    except Exception as e:
        logger.error(f"❌ [异常] 执行 Rclone 出错: {e}")

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
    logger.info(f"👀 监控服务已启动: {WATCH_DIR}")

# --- Web 面板 (读取 Rclone 日志) ---
@app.route('/')
def index():
    log_content = "暂无日志..."
    if os.path.exists(RCLONE_LOG_FILE):
        try:
            with open(RCLONE_LOG_FILE, 'r') as f:
                # 读取最后 3000 字符，避免页面卡顿
                log_content = f.read()[-3000:] 
        except Exception as e:
            log_content = f"读取日志出错: {e}"
            
    return render_template_string('''
        <html>
        <head>
            <title>NAS Rclone Pro</title>
            <meta charset="utf-8">
            <style>
                body{font-family:'Courier New', monospace; padding:20px; background:#1e1e1e; color:#e0e0e0;} 
                .box{background:#2d2d2d; padding:20px; border-radius:8px; box-shadow:0 4px 6px rgba(0,0,0,0.3);}
                h2{color:#4caf50; margin-top:0;}
                .status{font-size:14px; color:#888; margin-bottom:15px;}
                pre{background:#000; color:#0f0; padding:15px; overflow:auto; height:70vh; border:1px solid #444; border-radius:4px;}
            </style>
        </head>
        <body>
            <div class="box">
                <h2>🚀 飞牛 NAS Rclone 控制台</h2>
                <div class="status">
                    状态: <span style="color:#4caf50">● 运行中</span> | 
                    端口: <span style="color:#fff">{{ port }}</span> | 
                    监听目录: /watchdir
                </div>
                <h3>📜 实时上传日志 (Rclone)</h3>
                <pre>{{ logs }}</pre>
            </div>
        </body>
        </html>
    ''', logs=log_content, port=request.host.split(':')[-1])

if __name__ == "__main__":
    init_db()
    start_watcher()
    
    # === 关键修改：默认使用 5572 端口，避开 NAS 的 80 端口 ===
    port = int(os.getenv('PANEL_PORT', 5572))
    
    print("-" * 50)
    print(f"✅ Web 面板启动成功")
    print(f"👉 访问地址: http://[你的NAS_IP]:{port}")
    print("-" * 50)
    
    app.run(host='0.0.0.0', port=port)
