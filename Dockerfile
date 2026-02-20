FROM python:3.12-slim

# 设置工作目录
WORKDIR /app

# 复制依赖列表
COPY requirements.txt .

# 安装依赖管理工具 uv 并安装所需依赖
RUN pip install uv && \
    uv pip install --system -r requirements.txt

# 复制项目所有源文件
COPY . .

# 设置环境变量
ENV WEB_PORT=8812
ENV TZ=Asia/Shanghai
ENV PYTHONUNBUFFERED=1

# 暴露服务端口
EXPOSE 8812

# 设置数据目录为卷，确保数据库不会因为容器重建而丢失
VOLUME ["/app/data"]

# 启动应用
CMD ["python", "main.py"]
