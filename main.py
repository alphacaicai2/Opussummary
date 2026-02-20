#!/usr/bin/env python3
"""
OpusBrief - RSS 智能简报系统

Main CLI entry point for:
- Starting web UI
- Generating briefings
- Testing connections

Usage:
    python main.py              # Start web UI (default)
    python main.py web          # Web UI only
    python main.py generate     # Interactive briefing generation
    python main.py test-llm     # Test LLM connection
    python main.py test-miniflux # Test Miniflux connection
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

import uvicorn

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("opus")


def _parse_int_list(raw: str | None) -> list[int]:
    """
    Parse comma-separated integers with validation.

    Args:
        raw: Comma-separated string of integers (e.g., "1,2,3")

    Returns:
        List of positive integers

    Raises:
        ValueError: If any value is not a valid positive integer
    """
    if not raw:
        return []

    items: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue

        try:
            value = int(part)
        except ValueError:
            raise ValueError(f"Invalid integer value: '{part}'")

        if value <= 0:
            raise ValueError(f"Value must be a positive integer: {value}")

        items.append(value)

    return items


def _print_json(data: dict[str, Any]) -> None:
    """Print data as formatted JSON."""
    print(json.dumps(data, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    """Build argument parser."""
    parser = argparse.ArgumentParser(
        prog="opus",
        description="OpusBrief - RSS 智能简报系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python main.py                    # Start web UI on http://localhost:8090
    python main.py --port 3000        # Start web UI on port 3000
    python main.py generate           # Generate briefing interactively
    python main.py test-miniflux      # Test Miniflux connection

For more information, visit: https://github.com/your-repo/opusbrief
        """,
    )

    # Global options
    parser.add_argument(
        "--host",
        default=os.getenv("WEB_HOST", "127.0.0.1"),
        help="Web server host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("WEB_PORT", "8090")),
        help="Web server port (default: 8090)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload for development",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )

    # Subcommands
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # start command (alias for web, scheduler to be added later)
    subparsers.add_parser(
        "start",
        help="Start web UI (default)",
        description="Start the web administration interface.",
    )

    # web command
    subparsers.add_parser(
        "web",
        help="Start web UI",
        description="Start the web administration interface.",
    )

    # generate command
    p_generate = subparsers.add_parser(
        "generate",
        help="Generate briefing manually",
        description="Generate a briefing with specified parameters.",
    )
    p_generate.add_argument(
        "--task-id",
        type=int,
        default=None,
        help="Use existing task configuration",
    )
    p_generate.add_argument(
        "--template",
        choices=["general", "investment", "ai_product", "wechat_mp"],
        default=None,
        help="Briefing template to use",
    )
    p_generate.add_argument(
        "--time-range",
        type=int,
        default=24,
        dest="time_range_hours",
        help="Hours to look back (default: 24)",
    )
    p_generate.add_argument(
        "--categories",
        default=None,
        help="Category IDs (comma-separated, e.g., 1,2,3)",
    )
    p_generate.add_argument(
        "--webhooks",
        default=None,
        help="Webhook IDs (comma-separated, e.g., 1,2)",
    )
    p_generate.add_argument(
        "--llm-config",
        type=int,
        default=None,
        dest="llm_config_id",
        help="LLM configuration ID",
    )
    p_generate.add_argument(
        "--output",
        "-o",
        default=None,
        help="Output file for briefing content",
    )

    # test-llm command
    p_test_llm = subparsers.add_parser(
        "test-llm",
        help="Test LLM connection",
        description="Test connection to an LLM provider.",
    )
    p_test_llm.add_argument(
        "--llm-config-id",
        type=int,
        default=None,
        help="Test saved LLM configuration",
    )
    p_test_llm.add_argument(
        "--base-url",
        default=None,
        help="Override base URL",
    )
    p_test_llm.add_argument(
        "--api-key",
        default=None,
        help="Override API key",
    )
    p_test_llm.add_argument(
        "--model",
        default=None,
        help="Override model name",
    )

    # test-miniflux command
    p_test_miniflux = subparsers.add_parser(
        "test-miniflux",
        help="Test Miniflux connection",
        description="Test connection to Miniflux RSS reader.",
    )
    p_test_miniflux.add_argument(
        "--url",
        default=None,
        help="Override Miniflux URL",
    )
    p_test_miniflux.add_argument(
        "--token",
        default=None,
        help="Override API token",
    )

    # list command
    p_list = subparsers.add_parser(
        "list",
        help="List resources",
        description="List available resources (categories, tasks, etc.)",
    )
    p_list.add_argument(
        "resource",
        choices=["categories", "tasks", "llm-configs", "webhooks", "briefings"],
        help="Resource to list",
    )

    return parser


def run_web(host: str, port: int, reload: bool) -> int:
    """Run the web server."""
    logger.info(f"Starting OpusBrief web server on http://{host}:{port}")
    logger.info("Press Ctrl+C to stop")

    try:
        uvicorn.run(
            "web.server:create_app",
            factory=True,
            host=host,
            port=port,
            reload=reload,
            log_level="debug" if reload else "info",
        )
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    return 0


def run_generate(args: argparse.Namespace) -> int:
    """Generate a briefing."""
    from web.server import (
        GenerateRequest,
        bootstrap,
        generate_briefing,
    )

    bootstrap()

    # Parse and validate integer lists with error handling
    try:
        category_ids = _parse_int_list(args.categories) if args.categories else None
    except ValueError as e:
        logger.error(f"Invalid category IDs: {e}")
        return 1

    try:
        webhook_ids = _parse_int_list(args.webhooks) if args.webhooks else None
    except ValueError as e:
        logger.error(f"Invalid webhook IDs: {e}")
        return 1

    payload = GenerateRequest(
        task_id=args.task_id,
        template=args.template,
        category_ids=category_ids,
        time_range_hours=args.time_range_hours,
        llm_config_id=args.llm_config_id,
        webhook_ids=webhook_ids,
    )

    try:
        result = generate_briefing(payload)

        # Output to file if specified
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(result["content"])
            logger.info(f"Briefing saved to {args.output}")

        # Print summary
        print(f"\n{'='*60}")
        print(f"Briefing #{result['id']} generated successfully")
        print(f"{'='*60}")
        print(f"Template: {result['template']}")
        print(f"Articles: {result['article_count']}")
        print(f"Time range: {result['source_start']} to {result['source_end']}")

        if result.get("sent_to"):
            print(f"\nPush results:")
            for s in result["sent_to"]:
                status = "OK" if s["success"] else "FAILED"
                print(f"  - Webhook #{s['webhook_id']}: {status}")

        print(f"\nContent preview (first 500 chars):")
        print("-" * 40)
        print(result["content"][:500] + "..." if len(result["content"]) > 500 else result["content"])
        print("-" * 40)

        return 0
    except Exception as e:
        logger.error(f"Failed to generate briefing: {e}")
        return 1


def run_test_llm(args: argparse.Namespace) -> int:
    """Test LLM connection."""
    from web.server import (
        LLMTestRequest,
        bootstrap,
        test_llm_connection,
    )

    bootstrap()

    # Use environment variables as fallback for LLM configuration
    base_url = args.base_url
    api_key = args.api_key
    model = args.model

    # If no explicit values provided, try environment variables (SiliconFlow)
    if not base_url:
        base_url = os.getenv("SILICONFLOW_BASE_URL")
    if not api_key:
        api_key = os.getenv("SILICONFLOW_API_KEY")
    if not model:
        model = os.getenv("SILICONFLOW_MODEL")

    payload = LLMTestRequest(
        llm_config_id=args.llm_config_id,
        base_url=base_url,
        api_key=api_key,
        model=model,
    )

    result = test_llm_connection(payload)
    _print_json(result)

    return 0 if result.get("success") else 1


def run_test_miniflux(args: argparse.Namespace) -> int:
    """Test Miniflux connection."""
    from web.server import (
        MinifluxTestRequest,
        bootstrap,
        test_miniflux_connection,
    )

    bootstrap()

    payload = MinifluxTestRequest(
        url=args.url,
        token=args.token,
    )

    result = test_miniflux_connection(payload)
    _print_json(result)

    return 0 if result.get("success") else 1


def run_list(args: argparse.Namespace) -> int:
    """List resources."""
    from web.server import (
        bootstrap,
        _get_conn,
    )

    bootstrap()

    with _get_conn() as conn:
        if args.resource == "categories":
            from web.server import _resolve_miniflux_config, MinifluxClient
            try:
                url, token = _resolve_miniflux_config()
                client = MinifluxClient(base_url=url, api_token=token)
                try:
                    categories = client.get_categories()
                    print(f"\nFound {len(categories)} categories:\n")
                    for cat in categories:
                        print(f"  [{cat['id']}] {cat['title']}")
                finally:
                    client.close()
            except Exception as e:
                print(f"Error: {e}")
                return 1

        elif args.resource == "tasks":
            rows = conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
            print(f"\nFound {len(rows)} tasks:\n")
            for row in rows:
                status = "enabled" if row["enabled"] else "disabled"
                print(f"  [{row['id']}] {row['name']} ({row['template']}) - {status}")

        elif args.resource == "llm-configs":
            rows = conn.execute("SELECT * FROM llm_configs ORDER BY id").fetchall()
            print(f"\nFound {len(rows)} LLM configurations:\n")
            for row in rows:
                default = " (default)" if row["is_default"] else ""
                print(f"  [{row['id']}] {row['name']} - {row['provider']}/{row['model']}{default}")

        elif args.resource == "webhooks":
            rows = conn.execute("SELECT * FROM webhooks ORDER BY id").fetchall()
            print(f"\nFound {len(rows)} webhooks:\n")
            for row in rows:
                default = " (default)" if row["is_default"] else ""
                status = "enabled" if row["enabled"] else "disabled"
                print(f"  [{row['id']}] {row['name']} ({row['type']}) - {status}{default}")

        elif args.resource == "briefings":
            rows = conn.execute(
                "SELECT id, template, article_count, status, created_at FROM briefings ORDER BY id DESC LIMIT 20"
            ).fetchall()
            print(f"\nRecent briefings (max 20):\n")
            for row in rows:
                print(f"  [{row['id']}] {row['template']} - {row['article_count']} articles - {row['status']}")

    return 0


def main() -> int:
    """Main entry point."""
    parser = build_parser()
    args = parser.parse_args()

    # Set debug logging
    if args.debug:
        logging.getLogger("opus").setLevel(logging.DEBUG)

    # Default to 'start' if no command specified
    command = args.command or "start"

    if command in {"start", "web"}:
        return run_web(args.host, args.port, args.reload)

    if command == "generate":
        return run_generate(args)

    if command == "test-llm":
        return run_test_llm(args)

    if command == "test-miniflux":
        return run_test_miniflux(args)

    if command == "list":
        return run_list(args)

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
