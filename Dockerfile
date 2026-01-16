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

# 正常模式 (请把 tail -f 改回来)
CMD ["python", "main.py"]
