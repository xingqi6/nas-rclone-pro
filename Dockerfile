FROM rclone/rclone:latest as rclone-cli
FROM python:3.11-alpine

# 安装基础工具和依赖
RUN apk add --no-cache bash curl build-base linux-headers sqlite supervisor

# 从官方镜像复制 rclone 二进制文件
COPY --from=rclone-cli /usr/local/bin/rclone /usr/bin/rclone

# 设置工作目录
WORKDIR /app

# 复制依赖文件
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制核心代码
COPY . .

# 创建数据目录
RUN mkdir -p /watchdir /app/data /root/.config/rclone

# 设置环境变量默认值
ENV TZ=Asia/Shanghai
ENV CHECK_FILE_COMPLETE=true
ENV AUTO_DELETE_AFTER_UPLOAD=true

# 启动命令
CMD ["python", "main.py"]
