# ============================================================================
# USAGE EXAMPLE - Worker/Orchestrator Integration
# ============================================================================
# This file demonstrates how to use the new worker/orchestrator system
# for task routing, intent classification, and hybrid LLM execution.
# ============================================================================

"""
QUICK START:

1. Load configuration:
    from aiassistant.infra.config.app_config import CONFIG
    
2. Initialize factory:
    from aiassistant.core.agent_factory import AgentFactory
    factory = AgentFactory(CONFIG)
    
3. Create components:
    manager = factory.create_manager()
    offline_workers = factory.create_workers("offline")
    online_workers = factory.create_workers("online")
    
4. Initialize orchestrator:
    from aiassistant.core.orchestrator_new import Orchestrator, TaskContext
    orchestrator = Orchestrator(CONFIG, db=None, manager=manager, 
                                offline_workers=offline_workers,
                                online_workers=online_workers)
    
5. Route tasks:
    context = TaskContext(user_id=1, session_id="sess_001", mode="auto")
    result = orchestrator.route_task("open chrome browser", context)
    print(result.text)  # Response from worker
    print(result.actions)  # Tool calls made
    print(result.meta)  # Metadata (online/offline mode used)
"""

# Example implementation
def example_routing_flow():
    from aiassistant.infra.config.app_config import CONFIG
    from aiassistant.core.agent_factory import AgentFactory
    from aiassistant.core.orchestrator_new import Orchestrator, TaskContext
    
    # Initialize factory
    factory = AgentFactory(CONFIG, db=None)
    
    # Create manager and workers
    manager = factory.create_manager()
    offline_workers = factory.create_workers("offline")
    online_workers = factory.create_workers("online")
    
    # Create orchestrator
    orchestrator = Orchestrator(
        config=CONFIG,
        db=None,
        manager=manager,
        offline_workers=offline_workers,
        online_workers=online_workers
    )
    
    # Set mode (auto, online, offline)
    orchestrator.set_mode("offline")
    
    # Example queries
    queries = [
        "open chrome",  # -> "os" intent
        "create an excel spreadsheet",  # -> "office" intent
        "find my documents",  # -> "files" intent
        "hello how are you",  # -> "general" intent
    ]
    
    # Route each query
    for query in queries:
        print(f"\nQuery: {query}")
        context = TaskContext(user_id=1, session_id="example", mode="auto")
        result = orchestrator.route_task(query, context)
        print(f"Response: {result.text}")
        print(f"Intent: {result.meta.get('intent')}")
        print(f"Mode: {result.meta.get('mode')}")


# Example: Manual worker usage
def example_direct_worker():
    from aiassistant.core.agent_factory import AgentFactory, WorkerSpec
    from aiassistant.workers.offline_worker import OfflineWorker
    from aiassistant.infra.config.app_config import CONFIG
    
    # Create worker spec
    spec = WorkerSpec(
        name="Custom Worker",
        intent="general",
        description="Custom worker for testing",
        tools={},
        offline_pipeline={
            "parser": "qwen2.5:0.5b",
            "reasoner": "llama3.1:8b",
            "formatter": "qwen2.5:0.5b"
        },
        hybrid_pipeline={}
    )
    
    # Create offline worker
    worker = OfflineWorker(spec, spec.offline_pipeline, db=None, config=CONFIG)
    
    # Execute directly
    from aiassistant.core.orchestrator_new import TaskContext
    context = TaskContext(user_id=1, session_id="test", mode="offline")
    result = worker.execute("explain machine learning", context)
    print(result.text)


# Example: Intent classification
def example_classification():
    from aiassistant.core.agent_factory import Manager, DEFAULT_INTENTS
    
    manager = Manager(intents=DEFAULT_INTENTS)
    
    # Test classification
    queries = [
        ("open volume settings", "os"),
        ("create word document", "office"),
        ("send gmail", "web"),
        ("find pdf files", "files"),
        ("what is AI", "general"),
    ]
    
    for query, expected_intent in queries:
        classified = manager.classify(query, None)
        match = "✓" if classified == expected_intent else "✗"
        print(f"{match} '{query}' -> {classified} (expected {expected_intent})")


if __name__ == "__main__":
    print("=== Intent Classification ===")
    example_classification()
    
    print("\n=== Direct Worker ===")
    # example_direct_worker()  # Uncomment to test
    
    print("\n=== Orchestrator Routing ===")
    # example_routing_flow()  # Uncomment to test
