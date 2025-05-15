# Vitrus Python SDK

A Python client for multi Agent-World-Actor orchestration, with easy-to-use spatial perception Workflows.
For detailed documentation and more examples access [Vitrus Docs](https://vitrus.gitbook.io/docs/concepts).

We also provide a [Typescript SDK](https://github.com/vitrus-ai/vitrus-sdk-python#).

## Installation
```bash
pip install git+https://github.com/vitrus-ai/vitrus-sdk-python.git
```

Or by cloning the repo
```bash
git clone https://github.com/vitrus-ai/vitrus-sdk-python.git
cd vitrus-sdk-python
pip install -e .
```

## Authentication

[Get an API Key](https://app.vitrus.ai)

```python
import os
from vitrus import Vitrus

# Initialize the client
vitrus_client = Vitrus(
    api_key=os.environ.get("VITRUS_API_KEY")
)
```

## Workflows

```python
import asyncio

async def main():
    result = await vitrus_client.workflow(
        "hello-world", 
        params={"prompt": "hello from python!"}
    )
    print(result)

    workflows = await vitrus_client.list_workflows()
    print(workflows)

if __name__ == "__main__":
    asyncio.run(main())
```

## Actors and Agents

Create a world at [app.vitrus.ai](https://app.vitrus.ai).

```python
import asyncio
import os
from vitrus import Vitrus

async def run_actor_example():
    # Actor side
    actor_vitrus = Vitrus(
        api_key=os.environ.get("VITRUS_API_KEY"),
        world_id="<your-world-id>"
    )
    # Connect as this actor to register handlers
    # The actor_name in connect() should match the first arg to actor()
    await actor_vitrus.connect(actor_name="forest_python_actor") 

    forest_actor = await actor_vitrus.actor("forest_python_actor", metadata={"human": "Python Tom Hanks", "eyes": "blue"})

    async def on_walk(args):
        print(f"Python actor received walk command with args: {args}")
        return f"run forest_python_actor, run! Details: {args}"

    forest_actor.on("walk", on_walk)
    print(f"Python actor '{forest_actor.name}' is listening for 'walk' commands...")
    # Keep actor alive to listen for commands (in a real app, this would be a long-running process)
    # For this example, we'll just simulate it by not exiting immediately.
    # In a real scenario, actor_vitrus.keep_alive() or similar might be needed if disconnects are an issue.

async def run_agent_example():
    # Agent side
    agent_vitrus = Vitrus(
        api_key=os.environ.get("VITRUS_API_KEY"),
        world_id="<your-world-id>" # Must match actor's world_id
    )
    await agent_vitrus.connect() # Connect as an agent (no specific actor name)

    # Get a proxy to the actor
    actor_proxy = await agent_vitrus.actor("forest_python_actor")

    print(f"Agent is about to call 'walk' on '{actor_proxy.name}'")
    response = await actor_proxy.run("walk", {"direction": "forward", "speed": "fast"})
    print(f"Agent received response from actor: {response}")

    await agent_vitrus.disconnect()
    # Note: The actor_vitrus connection in run_actor_example should be managed separately.
    # If both run in the same script for testing, ensure actor_vitrus is disconnected too.

async def main():
    # It's generally better to run actor and agent in separate processes or manage their lifecycles carefully.
    # For this example, we'll run them sequentially for demonstration.
    # You'd typically have the actor running persistently.
    
    # print("--- Running Actor Example ---")
    # await run_actor_example() # Start this in one terminal
    
    # print("\n--- Running Agent Example ---")
    # await run_agent_example() # Start this in another terminal after actor is listening

    # For a combined example (ensure actor is setup first):
    # 1. Run the actor part and let it listen.
    # 2. Then run the agent part.

    # Simplified test for now:
    # Initialize the client for workflows
    vitrus_client = Vitrus(api_key=os.environ.get("VITRUS_API_KEY"))
    await vitrus_client.connect()
    print("Listing workflows:")
    workflows = await vitrus_client.list_workflows()
    # print(workflows)
    if workflows and workflows[0].get('function', {}).get('name'):
        wf_name = workflows[0]['function']['name']
        print(f"Running workflow: {wf_name}")
        result = await vitrus_client.workflow(wf_name, params={'prompt': 'Test from readme'})
        print(f"Workflow result: {result}")
    else:
        print("No workflows found or first workflow has no name.")
    await vitrus_client.disconnect()

if __name__ == "__main__":
    # Replace <your-world-id> with an actual world ID from app.vitrus.ai
    # Ensure VITRUS_API_KEY is set in your environment
    # To test actor/agent: 
    # 1. Uncomment and run `asyncio.run(run_actor_example())`
    # 2. In another terminal/script, uncomment and run `asyncio.run(run_agent_example())`
    asyncio.run(main()) 