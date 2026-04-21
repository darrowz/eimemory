from eimemory.api.runtime import Runtime


runtime = Runtime.create()
runtime.memory.ingest(
    text="Remember concise replies",
    memory_type="preference",
    title="Concise replies",
    scope={"agent_id": "example"},
)

bundle = runtime.memory.recall(
    query="concise replies",
    scope={"agent_id": "example"},
    task_context={"task_type": "example.demo"},
)

print(bundle.to_dict())
