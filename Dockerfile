FROM rclone/rclone:latest as rclone-cli
FROM python:3.11-alpine

# --- 核心修复 1: 强制 Python 不缓存日志 (让你能看到报错) ---
ENV PYTHONUNBUFFERED=1

# 安装基础工具
RUN apk add --no-cache bash curl build-base linux-headers sqlite supervisor

# 复制 Rclone
COPY --from=rclone-cli /usr/local/bin/rclone /usr/bin/rclone

# 设置环境
WORKDIR /app
ENV TZ=Asia/Shanghai

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制逻辑代码
COPY . .

# 创建必要目录
RUN mkdir -p /watchdir /app/data /root/.config/rclone

# --- 核心修复 2: 显式使用 -u 参数启动 ---
CMD ["python", "-u", "main.py"]
