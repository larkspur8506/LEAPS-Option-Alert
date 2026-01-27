FROM python:3.11-alpine

WORKDIR /app

# 安装系统依赖和时区数据
RUN apk add --no-cache gcc musl-dev tzdata

# 设置时区为美国东部时间
ENV TZ=America/New_York
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码（注意：.env文件被.dockerignore忽略，不会包含在镜像中）
COPY . .

# 创建数据目录
RUN mkdir -p data && chmod 777 data

# 设置健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD python -c "import requests; requests.get('http://localhost:8000/health', timeout=5)"

# 暴露端口
EXPOSE 8000

# 启动应用
# 注意：必需通过环境变量提供配置（如WECHAT_WEBHOOK_URL、POLYGON_API_KEY等）
# 可通过 --env-file 或 -e 参数设置
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
