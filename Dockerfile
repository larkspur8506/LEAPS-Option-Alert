FROM python:3.11-alpine

WORKDIR /app

# 安装系统依赖
RUN apk add --no-cache gcc musl-dev

# 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码（不包括.env，会被.dockerignore忽略）
COPY . .

# 创建数据目录
RUN mkdir -p data && chmod 777 data

# 设置健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD python -c "import requests; requests.get('http://localhost:8000/health', timeout=5)"

# 暴露端口
EXPOSE 8000

# 启动应用
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
