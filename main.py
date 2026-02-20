"""
Opus Relay - 主入口

Miniflux RSS → Opus Relay → Discord 推送

用法：
    uv run python main.py                  # 启动 Web UI + 轮询服务
    uv run python main.py web              # 仅启动 Web UI（不轮询）
    uv run python main.py poll-only        # 仅轮询（无 Web UI）
    uv run python main.py poll-once        # 执行一次轮询
    uv run python main.py test-translation # 测试翻译 API
    uv run python main.py test-miniflux    # 测试 Miniflux 连接
    uv run python main.py list-feeds       # 列出所有 Feed
"""

import sys
import json
import os
import signal
import logging

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("opus")

# 配置文件路径
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_config() -> dict:
    """加载配置文件"""
    if not os.path.exists(CONFIG_PATH):
        print(f"❌ 配置文件不存在: {CONFIG_PATH}")
        print(f"   请复制 config.example.json 为 config.json 并填写配置")
        sys.exit(1)

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    # 验证必填字段
    required = ["miniflux_url", "miniflux_token"]
    for key in required:
        if not config.get(key):
            print(f"❌ 配置缺少必填字段: {key}")
            sys.exit(1)

    return config


def cmd_start(config: dict):
    """启动 Web UI + 轮询服务（默认模式）"""
    import threading
    import uvicorn
    from database import init_db
    from scheduler import PollScheduler
    from web_server import set_scheduler

    init_db()
    scheduler = PollScheduler(config)
    set_scheduler(scheduler)  # 注入到 web_server 供热重载使用

    # 优雅停机
    def handle_signal(sig, frame):
        scheduler.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    port = 8090
    print("🚀 Opus Relay 启动中...")
    print(f"   Miniflux: {config['miniflux_url']}")
    print(f"   轮询间隔: {config.get('poll_interval_minutes', 15)} 分钟")
    print(f"   路由数量: {len(config.get('routes', {}))}")
    print(f"   翻译: {'✅ 已配置' if config.get('translation', {}).get('base_url') else '❌ 未配置'}")
    print(f"   Web UI: http://localhost:{port}")
    print()

    # 轮询在后台线程运行
    poll_thread = threading.Thread(target=scheduler.run, daemon=True)
    poll_thread.start()

    # Web UI 在主线程运行（uvicorn）
    uvicorn.run("web_server:app", host="0.0.0.0", port=port, log_level="warning")


def cmd_poll_only(config: dict):
    """仅启动轮询服务（无 Web UI）"""
    from database import init_db
    from scheduler import PollScheduler

    init_db()
    scheduler = PollScheduler(config)

    def handle_signal(sig, frame):
        scheduler.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    print("🚀 Opus Relay 启动中（仅轮询）...")
    print(f"   Miniflux: {config['miniflux_url']}")
    print(f"   轮询间隔: {config.get('poll_interval_minutes', 15)} 分钟")
    print()

    scheduler.run()


def cmd_test_translation(config: dict):
    """测试翻译 API"""
    from database import init_db

    init_db()

    translation_cfg = config.get("translation", {})
    if not translation_cfg.get("base_url") or not translation_cfg.get("api_key"):
        print("❌ 翻译 API 未配置，请在 config.json 中填写 translation 字段")
        return

    from translator import Translator
    translator = Translator(
        base_url=translation_cfg["base_url"],
        api_key=translation_cfg["api_key"],
        model=translation_cfg.get("model", "gpt-4o-mini"),
    )
    translator.test_connection()
    translator.close()


def cmd_test_miniflux(config: dict):
    """测试 Miniflux 连接"""
    from miniflux_client import MinifluxClient
    client = MinifluxClient(
        base_url=config["miniflux_url"],
        api_token=config["miniflux_token"],
    )
    if client.test_connection():
        print("✅ Miniflux 连接成功！")
    else:
        print("❌ Miniflux 连接失败")
    client.close()


def cmd_poll_once(config: dict):
    """执行一次轮询"""
    from database import init_db
    from scheduler import PollScheduler

    init_db()
    scheduler = PollScheduler(config)
    count = scheduler.poll_once()
    print(f"✅ 轮询完成，推送了 {count} 条新条目")
    scheduler.stop()


def cmd_list_feeds(config: dict):
    """列出所有 Feed（方便配置路由）"""
    from miniflux_client import MinifluxClient
    client = MinifluxClient(
        base_url=config["miniflux_url"],
        api_token=config["miniflux_token"],
    )

    feeds = client.get_feeds()
    if not feeds:
        print("❌ 获取 Feed 列表失败或为空")
        client.close()
        return

    print(f"\n📋 共 {len(feeds)} 个 Feed:\n")
    print(f"{'Feed ID':<10} {'分组ID':<8} {'分组':<20} {'Feed 名称'}")
    print("-" * 80)

    for feed in sorted(feeds, key=lambda f: f.get("category", {}).get("title", "")):
        feed_id = feed.get("id", "?")
        category_id = feed.get("category", {}).get("id", "?")
        category = feed.get("category", {}).get("title", "未分组")
        title = feed.get("title", "无标题")
        print(f"{feed_id:<10} {category_id:<8} {category:<20} {title}")

    print(f"\n💡 在 config.json 的 routes 中按分组ID配置：")
    print(f'   例如: "routes": {{ "{feeds[0].get("category", {}).get("id", "2")}": "https://discord.com/api/webhooks/..." }}')
    print(f'   也支持精确到某个 Feed: "feed:1": "https://discord.com/api/webhooks/..."')
    print(f'   通配所有未匹配的: "*": "https://discord.com/api/webhooks/..."')

    client.close()


def cmd_web():
    """启动配置管理 Web UI"""
    import uvicorn

    port = 8090
    print(f"🌐 Opus Relay 配置管理 UI 启动中...")
    print(f"   访问: http://localhost:{port}")
    print(f"   API 文档: http://localhost:{port}/docs")
    print()

    uvicorn.run("web_server:app", host="0.0.0.0", port=port, log_level="info")


def main():
    """主入口"""
    command = sys.argv[1] if len(sys.argv) > 1 else "start"

    # web 命令不需要加载配置
    if command == "web":
        cmd_web()
        return

    config = load_config()

    commands = {
        "start": cmd_start,
        "poll-only": cmd_poll_only,
        "test-translation": cmd_test_translation,
        "test-miniflux": cmd_test_miniflux,
        "poll-once": cmd_poll_once,
        "list-feeds": cmd_list_feeds,
    }

    if command in commands:
        commands[command](config)
    else:
        print(f"❌ 未知命令: {command}")
        print(f"\n可用命令：")
        print(f"  start            启动 Web UI + 轮询服务（默认）")
        print(f"  web              仅启动 Web UI")
        print(f"  poll-only        仅轮询（无 Web UI）")
        print(f"  test-translation 测试翻译 API")
        print(f"  test-miniflux    测试 Miniflux 连接")
        print(f"  poll-once        执行一次轮询")
        print(f"  list-feeds       列出所有 Feed")
        sys.exit(1)


if __name__ == "__main__":
    main()
