FROM rclone/rclone:latest as rclone-cli
FROM python:3.11-alpine

ENV PYTHONUNBUFFERED=1

RUN apk add --no-cache bash curl build-base linux-headers sqlite supervisor

COPY --from=rclone-cli /usr/local/bin/rclone /usr/bin/rclone

WORKDIR /app
ENV TZ=Asia/Shanghai

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /watchdir /app/data /root/.config/rclone

# 调试模式：什么都不做，只是挂机，防止重启
CMD ["tail", "-f", "/dev/null"]
