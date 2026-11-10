"""Test helper for assertions about a completed tick and its optional pass."""


async def tick(agent):
    await agent.heartbeat.coro(agent)
    if agent.background_task is not None:
        await agent.background_task
