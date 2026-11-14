"""Resume a failed workflow and print which stages were reused.

    docker compose --profile full exec api python /app/scripts/retry_workflow.py <WORKFLOW_ID>

This drives the pipeline exactly like the worker does (real DB, real providers), so it verifies that
a retry skips planning, script, storyboard and narration that an earlier attempt already produced.
"""

from __future__ import annotations

import argparse
import asyncio
import uuid

from sqlalchemy import select

from app.db.session import SessionFactory
from app.models import ProductionWorkflow, WorkflowLog
from app.workflow.pipeline import ProductionPipeline


async def run(workflow_id: uuid.UUID) -> int:
    async with SessionFactory() as session:
        workflow = await session.get(ProductionWorkflow, workflow_id)
        if workflow is None:
            print(f"workflow {workflow_id} not found")
            return 2
        print(f"before: status={workflow.status} stage={workflow.stage} "
              f"attempts={workflow.retry_count}")
        workflow.retry_count += 1
        workflow.status = "pending"
        workflow.error_message = None
        await session.commit()

        pipeline = ProductionPipeline(session)
        try:
            print("run result:", await pipeline.run(workflow_id, stop_at_approval=True))
        except Exception as exc:  # noqa: BLE001 - the point is to report the failure
            print(f"run raised: {type(exc).__name__}: {str(exc)[:300]}")

    async with SessionFactory() as session:
        logs = list(
            (
                await session.execute(
                    select(WorkflowLog)
                    .where(WorkflowLog.workflow_id == workflow_id)
                    .order_by(WorkflowLog.at)
                )
            ).scalars()
        )
        print("--- log tail ---")
        for entry in logs[-16:]:
            print(f"  [{entry.level}] {entry.message[:150]}")
        workflow = await session.get(ProductionWorkflow, workflow_id)
        print(f"after: status={workflow.status} stage={workflow.stage} "
              f"cost=${float(workflow.cost_usd or 0):.4f}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow_id", help="production_workflows.id")
    raise SystemExit(asyncio.run(run(uuid.UUID(parser.parse_args().workflow_id))))
