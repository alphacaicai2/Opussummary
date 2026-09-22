FROM python:3.12-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码与静态资源
COPY *.py .
COPY static/ /app/static/

# 数据目录
RUN mkdir -p /app/data
VOLUME /app/data

# 配置文件通过挂载注入
VOLUME /app/config.json

CMD ["python", "main.py"]
