"""
OpusBrief - Briefing Scheduler

Responsible for:
- Scheduling briefing generation tasks with timezone support
- Supporting cron expressions and simple time-based scheduling
- Triggering BriefingGenerator and pushing results to webhooks
"""

from __future__ import annotations

import inspect
import logging
import re
from datetime import datetime
from threading import RLock
from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

logger = logging.getLogger("opus.briefing.scheduler")


# =============================================================================
# Constants
# =============================================================================

SUPPORTED_TIMEZONES: set[str] = {
    "Asia/Shanghai",
    "America/New_York",
    "America/Los_Angeles",
    "Europe/London",
}

JOB_ID_PREFIX = "briefing_task_"
TIME_PATTERN = re.compile(r"^(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?$")
DATETIME_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
]


# =============================================================================
# Exceptions
# =============================================================================


class SchedulerError(Exception):
    """Base exception for scheduler errors."""

    pass


class ScheduleParseError(SchedulerError):
    """Raised when a schedule expression cannot be parsed."""

    pass


class TaskExecutionError(SchedulerError):
    """Raised when a scheduled task fails to execute."""

    pass


class TimezoneNotSupportedError(SchedulerError):
    """Raised when an unsupported timezone is specified."""

    pass


# =============================================================================
# Protocols
# =============================================================================


@runtime_checkable
class BriefingGeneratorProtocol(Protocol):
    """Protocol for BriefingGenerator to enable duck typing."""

    def generate_for_task(self, task: Any) -> Any:
        """Generate briefing content for a specific task."""
        ...


@runtime_checkable
class WebhookProtocol(Protocol):
    """Protocol for webhook configuration."""

    @property
    def id(self) -> int:
        ...

    @property
    def type(self) -> str:
        ...

    @property
    def url(self) -> str:
        ...


@runtime_checkable
class BriefingTaskProtocol(Protocol):
    """Protocol for briefing task configuration."""

    @property
    def id(self) -> int:
        ...

    @property
    def name(self) -> str:
        ...

    @property
    def enabled(self) -> bool:
        ...

    @property
    def schedule(self) -> str:
        ...

    @property
    def timezone(self) -> str:
        ...

    def get_webhook_ids(self) -> list[int]:
        ...


# =============================================================================
# Main Scheduler Class
# =============================================================================


class BriefingScheduler:
    """
    Database-driven briefing scheduler using APScheduler.

    Supported schedule formats:
        - Cron expression: "cron:*/30 * * * *" or "*/30 * * * *"
        - Daily fixed time: "at:09:30" or "09:30"
        - One-time datetime: "once:2026-02-20 09:30:00"

    Supported timezones:
        - Asia/Shanghai
        - America/New_York
        - America/Los_Angeles
        - Europe/London

    Example:
        >>> scheduler = BriefingScheduler()
        >>> scheduler.start()
        >>> scheduler.add_task(task_id=1)
        >>> scheduler.shutdown()
    """

    def __init__(
        self,
        generator: Any | None = None,
        *,
        sender_factory: Callable[[str], Any] | None = None,
        default_timezone: str = "Asia/Shanghai",
        misfire_grace_time: int = 120,
        max_instances: int = 1,
    ) -> None:
        """
        Initialize the scheduler.

        Args:
            generator: BriefingGenerator instance for generating briefings.
            sender_factory: Factory function to create sender instances from webhook URLs.
            default_timezone: Default timezone for tasks without explicit timezone.
            misfire_grace_time: Seconds after scheduled time to still run missed jobs.
            max_instances: Maximum concurrent instances of the same job.
        """
        self._lock = RLock()
        self._default_timezone = self._validate_timezone(default_timezone)
        self._default_tz = self._load_timezone(self._default_timezone)

        # Lazy import generator to avoid circular imports
        self._generator = generator
        self._sender_factory = sender_factory

        # Scheduler configuration
        self._scheduler = BackgroundScheduler(
            timezone=self._default_tz,
            job_defaults={
                "coalesce": True,
                "max_instances": max_instances,
                "misfire_grace_time": misfire_grace_time,
            },
        )
        self._is_running = False

    # =========================================================================
    # Lifecycle Methods
    # =========================================================================

    def start(self, *, load_from_database: bool = True) -> None:
        """
        Start the scheduler.

        Args:
            load_from_database: Whether to load enabled tasks from database on start.
        """
        with self._lock:
            if self._is_running:
                logger.warning("Scheduler is already running")
                return

            self._scheduler.start()
            self._is_running = True
            logger.info("BriefingScheduler started (timezone=%s)", self._default_timezone)

        if load_from_database:
            self.reload_enabled_tasks()

    def shutdown(self, *, wait: bool = True) -> None:
        """
        Shutdown the scheduler.

        Args:
            wait: Whether to wait for running jobs to complete.
        """
        with self._lock:
            if not self._is_running:
                return

            self._scheduler.shutdown(wait=wait)
            self._is_running = False
            logger.info("BriefingScheduler stopped")

    def is_running(self) -> bool:
        """Check if the scheduler is running."""
        return self._is_running

    # =========================================================================
    # Task Management Methods
    # =========================================================================

    def add_task(self, task_or_id: Any) -> str:
        """
        Add a new scheduled task.

        Args:
            task_or_id: BriefingTask instance or task ID.

        Returns:
            The job ID of the scheduled task.

        Raises:
            ValueError: If task is disabled or has invalid configuration.
            ScheduleParseError: If the schedule expression is invalid.
        """
        task = self._resolve_task(task_or_id)

        if not task.enabled:
            raise ValueError(f"Task {task.id} is disabled and cannot be scheduled")

        trigger = self._build_trigger(
            schedule_expr=task.schedule or "",
            tz_name=task.timezone or self._default_timezone,
        )

        job_id = self._make_job_id(task.id)
        job_name = task.name or f"Briefing Task {task.id}"

        with self._lock:
            job = self._scheduler.add_job(
                func=self._execute_task,
                trigger=trigger,
                args=[task.id],
                id=job_id,
                name=job_name,
                replace_existing=True,
            )

        logger.info(
            "Scheduled task: id=%s name='%s' next_run=%s",
            task.id,
            job_name,
            job.next_run_time,
        )
        return job.id

    def remove_task(self, task_id: int) -> bool:
        """
        Remove a scheduled task.

        Args:
            task_id: The ID of the task to remove.

        Returns:
            True if the task was removed, False if it was not found.
        """
        job_id = self._make_job_id(task_id)

        with self._lock:
            try:
                self._scheduler.remove_job(job_id)
                logger.info("Removed scheduled task: id=%s", task_id)
                return True
            except JobLookupError:
                logger.debug("Task %s was not found in scheduler", task_id)
                return False

    def update_task(self, task_or_id: Any) -> str | None:
        """
        Update an existing scheduled task.

        If the task is disabled, it will be removed from the scheduler.

        Args:
            task_or_id: BriefingTask instance or task ID.

        Returns:
            The job ID if scheduled, None if removed.
        """
        task = self._resolve_task(task_or_id)

        if not task.enabled:
            self.remove_task(task.id)
            logger.info("Task %s is disabled, removed from scheduler", task.id)
            return None

        return self.add_task(task)

    def reload_enabled_tasks(self) -> tuple[int, int]:
        """
        Reload all enabled tasks from the database.

        This will:
        - Add new tasks that are enabled but not scheduled
        - Remove tasks that are no longer in the database or are disabled
        - Update tasks that have changed

        Returns:
            Tuple of (loaded_count, removed_count).
        """
        try:
            from database import list_briefing_tasks
        except ImportError as e:
            logger.error("Failed to import database module: %s", e)
            return 0, 0

        # Fetch enabled tasks from database
        try:
            tasks = list_briefing_tasks(enabled=True, limit=10000, offset=0)
        except Exception as e:
            logger.error("Failed to fetch tasks from database: %s", e)
            return 0, 0

        desired_task_ids = {task.id for task in tasks}

        # Remove orphaned jobs
        removed_count = 0
        with self._lock:
            for job in self._scheduler.get_jobs():
                task_id = self._extract_task_id_from_job_id(job.id)
                if task_id is not None and task_id not in desired_task_ids:
                    if self.remove_task(task_id):
                        removed_count += 1

        # Add/update desired tasks
        loaded_count = 0
        for task in tasks:
            try:
                self.add_task(task)
                loaded_count += 1
            except (ValueError, ScheduleParseError, TimezoneNotSupportedError) as e:
                logger.warning("Failed to schedule task %s: %s", task.id, e)

        logger.info(
            "Scheduler reload complete: loaded=%d removed=%d",
            loaded_count,
            removed_count,
        )
        return loaded_count, removed_count

    # =========================================================================
    # Job Inspection Methods
    # =========================================================================

    def list_jobs(self) -> list[dict[str, Any]]:
        """
        List all scheduled jobs.

        Returns:
            List of job information dictionaries.
        """
        jobs = []
        with self._lock:
            for job in self._scheduler.get_jobs():
                jobs.append(
                    {
                        "id": job.id,
                        "name": job.name,
                        "next_run_time": (
                            job.next_run_time.isoformat() if job.next_run_time else None
                        ),
                        "trigger": str(job.trigger),
                    }
                )
        return jobs

    def get_next_run_time(self, task_id: int) -> datetime | None:
        """
        Get the next scheduled run time for a task.

        Args:
            task_id: The task ID.

        Returns:
            The next run time or None if not scheduled.
        """
        job_id = self._make_job_id(task_id)
        with self._lock:
            job = self._scheduler.get_job(job_id)
            return job.next_run_time if job else None

    # =========================================================================
    # Task Execution
    # =========================================================================

    def _execute_task(self, task_id: int) -> None:
        """
        Execute a scheduled task.

        This method is called by APScheduler when a task's trigger fires.

        Args:
            task_id: The ID of the task to execute.
        """
        logger.info("Executing scheduled task: id=%s", task_id)

        try:
            from database import get_briefing_task
        except ImportError as e:
            logger.error("Failed to import database module: %s", e)
            return

        # Fetch fresh task data from database
        task = get_briefing_task(task_id)
        if task is None:
            logger.warning("Task %s not found in database, skipping", task_id)
            return

        if not task.enabled:
            logger.info("Task %s is disabled, skipping execution", task_id)
            return

        try:
            # Generate briefing content
            content = self._generate_briefing(task)

            if not content:
                logger.warning("Task %s produced empty content", task_id)
                return

            # Send to webhooks
            sent_count = self._send_to_webhooks(task, content)

            # Log result with appropriate level based on sent count
            if sent_count > 0:
                logger.info(
                    "Task %s completed successfully: sent_to=%d webhook(s)",
                    task_id,
                    sent_count,
                )
            else:
                logger.warning(
                    "Task %s completed but no webhooks received the briefing (sent=0)",
                    task_id,
                )

        except TaskExecutionError:
            raise
        except Exception as e:
            logger.exception("Task %s execution failed: %s", task_id, e)
            raise TaskExecutionError(f"Task {task_id} execution failed") from e

    def _generate_briefing(self, task: Any) -> str:
        """
        Generate briefing content for a task.

        Args:
            task: The briefing task configuration.

        Returns:
            The generated briefing content as a string.

        Raises:
            TaskExecutionError: If briefing generation fails.
        """
        generator = self._get_generator()

        # Try different method names for compatibility
        method_names = ["generate_for_task", "generate_briefing", "generate"]

        for method_name in method_names:
            method = getattr(generator, method_name, None)
            if callable(method):
                try:
                    result = self._call_generator_method(method, task)
                    return self._extract_content(result)
                except Exception as e:
                    logger.warning(
                        "Generator method '%s' failed: %s",
                        method_name,
                        e,
                    )
                    continue

        raise TaskExecutionError("No compatible generator method found")

    def _get_generator(self) -> Any:
        """Get or create the briefing generator instance."""
        if self._generator is None:
            try:
                from briefing.generator import BriefingGenerator

                self._generator = BriefingGenerator()
            except ImportError as e:
                raise TaskExecutionError(
                    f"Failed to import BriefingGenerator: {e}"
                ) from e
        return self._generator

    @staticmethod
    def _call_generator_method(method: Callable[..., Any], task: Any) -> Any:
        """Call a generator method with appropriate arguments."""
        sig = inspect.signature(method)
        params = sig.parameters

        # Try different parameter names
        if "task" in params:
            return method(task=task)
        if "task_id" in params:
            return method(task_id=task.id)

        # Fall back to positional arguments
        param_count = len(params)
        if param_count == 0:
            return method()
        if param_count == 1:
            return method(task)

        return method(task)

    @staticmethod
    def _extract_content(result: Any) -> str:
        """Extract string content from generator result."""
        if isinstance(result, str):
            content = result.strip()
            if content:
                return content

        if isinstance(result, dict):
            for key in ("content", "briefing_content", "markdown", "text"):
                value = result.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()

        # Try object attributes
        for attr in ("content", "briefing_content", "markdown", "text"):
            value = getattr(result, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()

        raise TaskExecutionError("Generator result does not contain valid text content")

    def _send_to_webhooks(self, task: Any, content: str) -> int:
        """
        Send briefing content to configured webhooks.

        Args:
            task: The briefing task configuration.
            content: The briefing content to send.

        Returns:
            Number of webhooks that received the content.
        """
        try:
            from database import get_default_webhook, get_webhook
        except ImportError as e:
            logger.error("Failed to import database module: %s", e)
            return 0

        # Get webhook IDs from task
        webhook_ids = task.get_webhook_ids() if hasattr(task, "get_webhook_ids") else []

        # Fall back to default webhook
        if not webhook_ids:
            default_webhook = get_default_webhook()
            if default_webhook:
                webhook_ids = [default_webhook.id]

        if not webhook_ids:
            logger.warning("Task %s has no webhooks configured", task.id)
            return 0

        sent_count = 0
        seen_ids = set()

        for webhook_id in webhook_ids:
            # Skip duplicates
            if webhook_id in seen_ids:
                continue
            seen_ids.add(webhook_id)

            webhook = get_webhook(webhook_id)
            if webhook is None:
                logger.warning("Webhook %s not found, skipping", webhook_id)
                continue

            if not webhook.url:
                logger.warning("Webhook %s has empty URL, skipping", webhook_id)
                continue

            webhook_type = (webhook.type or "").lower()
            if webhook_type != "discord":
                logger.warning(
                    "Webhook %s has unsupported type '%s', skipping",
                    webhook_id,
                    webhook_type,
                )
                continue

            # Send via Discord sender
            sender = None
            try:
                sender = self._create_sender(webhook.url)
                messages_sent = sender.send_briefing(content)
                if messages_sent > 0:
                    sent_count += 1
                    logger.info("Sent briefing to webhook %s (%d message(s))", webhook_id, messages_sent)
                else:
                    logger.warning("Failed to send briefing to webhook %s: no messages were sent", webhook_id)
            except Exception as e:
                logger.exception("Failed to send to webhook %s: %s", webhook_id, e)
            finally:
                if sender is not None:
                    self._close_sender(sender)

        return sent_count

    def _create_sender(self, webhook_url: str) -> Any:
        """Create a sender instance for a webhook URL."""
        if self._sender_factory:
            return self._sender_factory(webhook_url)

        from sender.discord import DiscordSender

        return DiscordSender(webhook_url)

    @staticmethod
    def _close_sender(sender: Any) -> None:
        """Close a sender instance if it has a close method."""
        close_method = getattr(sender, "close", None)
        if callable(close_method):
            try:
                close_method()
            except Exception as e:
                logger.warning("Failed to close sender: %s", e)

    # =========================================================================
    # Trigger Building
    # =========================================================================

    def _build_trigger(self, schedule_expr: str, tz_name: str) -> BaseTrigger:
        """
        Build an APScheduler trigger from a schedule expression.

        Args:
            schedule_expr: The schedule expression string.
            tz_name: The timezone name.

        Returns:
            An APScheduler trigger instance.

        Raises:
            ScheduleParseError: If the expression cannot be parsed.
        """
        expr = (schedule_expr or "").strip()

        if not expr:
            raise ScheduleParseError("Schedule expression cannot be empty")

        expr_lower = expr.lower()
        if expr_lower in {"manual", "disabled", "none"}:
            raise ScheduleParseError(f"Schedule mode '{expr}' is not schedulable")

        tz = self._load_timezone(self._validate_timezone(tz_name))

        # Handle prefixed expressions
        if expr_lower.startswith("cron:"):
            return self._build_cron_trigger(expr[5:].strip(), tz)

        if expr_lower.startswith("at:"):
            hour, minute, second = self._parse_time(expr[3:].strip())
            return CronTrigger(hour=hour, minute=minute, second=second, timezone=tz)

        if expr_lower.startswith("once:"):
            run_at = self._parse_datetime(expr[5:].strip(), tz)
            return DateTrigger(run_date=run_at, timezone=tz)

        # Try to infer expression type
        if self._is_cron_expression(expr):
            return self._build_cron_trigger(expr, tz)

        if TIME_PATTERN.fullmatch(expr):
            hour, minute, second = self._parse_time(expr)
            return CronTrigger(hour=hour, minute=minute, second=second, timezone=tz)

        # Try to parse as datetime for one-time execution
        run_at = self._parse_datetime(expr, tz)
        return DateTrigger(run_date=run_at, timezone=tz)

    @staticmethod
    def _build_cron_trigger(expr: str, tz: "ZoneInfo") -> CronTrigger:
        """Build a cron trigger from a crontab expression."""
        try:
            return CronTrigger.from_crontab(expr, timezone=tz)
        except ValueError as e:
            raise ScheduleParseError(f"Invalid cron expression '{expr}': {e}") from e

    @staticmethod
    def _is_cron_expression(expr: str) -> bool:
        """Check if expression looks like a 5-field cron expression."""
        parts = expr.split()
        return len(parts) == 5

    @staticmethod
    def _parse_time(value: str) -> tuple[int, int, int]:
        """
        Parse a time string into hour, minute, second.

        Args:
            value: Time string like "09:30" or "09:30:00".

        Returns:
            Tuple of (hour, minute, second).

        Raises:
            ScheduleParseError: If the time string is invalid.
        """
        match = TIME_PATTERN.fullmatch(value)
        if not match:
            raise ScheduleParseError(f"Invalid time format '{value}'. Expected HH:MM or HH:MM:SS")

        hour = int(match.group("hour"))
        minute = int(match.group("minute"))
        second = int(match.group("second") or 0)

        if not (0 <= hour <= 23):
            raise ScheduleParseError(f"Hour {hour} out of range (0-23)")
        if not (0 <= minute <= 59):
            raise ScheduleParseError(f"Minute {minute} out of range (0-59)")
        if not (0 <= second <= 59):
            raise ScheduleParseError(f"Second {second} out of range (0-59)")

        return hour, minute, second

    @staticmethod
    def _parse_datetime(value: str, tz: "ZoneInfo") -> datetime:
        """
        Parse a datetime string.

        Args:
            value: Datetime string.
            tz: Timezone to use if not specified in the string.

        Returns:
            A timezone-aware datetime.

        Raises:
            ScheduleParseError: If the datetime string is invalid.
        """
        raw = value.strip()
        if not raw:
            raise ScheduleParseError("Datetime string cannot be empty")

        # Try ISO format first
        for candidate in (raw, raw.replace(" ", "T")):
            try:
                parsed = datetime.fromisoformat(candidate)
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=tz)
                return parsed.astimezone(tz)
            except ValueError:
                continue

        # Try other common formats
        for fmt in DATETIME_FORMATS:
            try:
                parsed = datetime.strptime(raw, fmt)
                return parsed.replace(tzinfo=tz)
            except ValueError:
                continue

        raise ScheduleParseError(
            f"Invalid datetime format '{value}'. "
            f"Expected ISO format or one of: {', '.join(DATETIME_FORMATS)}"
        )

    # =========================================================================
    # Utility Methods
    # =========================================================================

    @staticmethod
    def _make_job_id(task_id: int) -> str:
        """Create a job ID from a task ID."""
        return f"{JOB_ID_PREFIX}{task_id}"

    @staticmethod
    def _extract_task_id_from_job_id(job_id: str) -> int | None:
        """Extract task ID from a job ID, or None if not a task job."""
        if not job_id.startswith(JOB_ID_PREFIX):
            return None
        suffix = job_id[len(JOB_ID_PREFIX) :]
        return int(suffix) if suffix.isdigit() else None

    @staticmethod
    def _resolve_task(task_or_id: Any) -> Any:
        """
        Resolve a task from a task object or ID.

        Args:
            task_or_id: BriefingTask instance or integer ID.

        Returns:
            A BriefingTask instance.

        Raises:
            KeyError: If the task cannot be found.
        """
        if hasattr(task_or_id, "id") and hasattr(task_or_id, "schedule"):
            return task_or_id

        try:
            from database import get_briefing_task

            task = get_briefing_task(int(task_or_id))
            if task is None:
                raise KeyError(f"Task {task_or_id} not found in database")
            return task
        except ImportError as e:
            raise KeyError(f"Cannot resolve task: database module not available: {e}") from e

    @staticmethod
    def _validate_timezone(tz_name: str) -> str:
        """Validate that a timezone is supported."""
        if tz_name not in SUPPORTED_TIMEZONES:
            raise TimezoneNotSupportedError(
                f"Timezone '{tz_name}' is not supported. "
                f"Supported timezones: {', '.join(sorted(SUPPORTED_TIMEZONES))}"
            )
        return tz_name

    @staticmethod
    def _load_timezone(tz_name: str) -> "ZoneInfo":
        """Load a ZoneInfo object for a timezone name."""
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            return ZoneInfo(tz_name)
        except ZoneInfoNotFoundError as e:
            raise TimezoneNotSupportedError(f"Timezone '{tz_name}' not found: {e}") from e
