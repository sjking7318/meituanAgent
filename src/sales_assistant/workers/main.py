from __future__ import annotations

import structlog

logger = structlog.get_logger()


def run() -> None:
    """Async worker entrypoint (ingestion/memory/distillation/eval).

    Placeholder for M2+ Kafka consumers. The worker process is separate from
    the API process (see architecture.md 3.2). Consumers will be wired here as
    the corresponding milestones land.
    """
    logger.info("worker_entrypoint_not_yet_implemented")


if __name__ == "__main__":
    run()
