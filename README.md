# Vitrus Python SDK

A Python client for multi Agent-World-Actor orchestration, with easy-to-use spatial perception _Workflows_.
For detailed documentation and more examples access [Vitrus Docs](https://vitrus.gitbook.io/docs/concepts).

💡 Tip: If anything takes more than 2 minutes to setup, ask in our [Discord channel](https://discord.gg/Xd5f6WSh).

## Installation

```bash
# Using pip
pip install vitrus

# Or install from source
pip install git+https://github.com/vitrus/vitrus-sdk-python.git
```

## Authentication

[Get an API Key](https://app.vitrus.ai)

```python
import asyncio
from vitrus import Vitrus

async def main():
    # Initialize the client with all options
    vitrus = Vitrus(
        api_key="YOUR_API_KEY",
        # Optional parameters
        world="your_world_id",  # Required for actors
        base_url="wss://vitrus-dao.onrender.com",  # Default value
        debug=False  # Set to True for debugging
    )
    
    # Now you can use vitrus methods
    
asyncio.run(main())
```

# Workflows

**Workflows** have a similar schema as AI tools, so it connects perfectly with [OpenAI function Calling](https://platform.openai.com/docs/guides/function-calling?api-mode=chat). Making Workflows great to compose complex physical tasks with AI Agents. The following is a simple example of how to run a workflow:

```python
import asyncio
from vitrus import Vitrus

async def main():
    vitrus = Vitrus(api_key="YOUR_API_KEY")
    
    # running a basic workflow
    result = await vitrus.workflow("hello-world", {
        "prompt": "hello world!"
    })
    
    print(result)
    
asyncio.run(main())
```

Workflows are executed in Cloud GPUs (e.g. `Nvidia A100`), and combine multiple AI models for complex tasks.

## Available Workflows

We are continously updating the available workflows, and keeping them up to date with state-of-the-art (SOTA) AI models. For the latest list of workflows, you can execute:

```python
import asyncio
from vitrus import Vitrus

async def main():
    vitrus = Vitrus(api_key="YOUR_API_KEY")
    workflows = await vitrus.list_workflows()
    print(workflows)
    
asyncio.run(main())
```

# Worlds and Actors

Create a world at [app.vitrus.ai](https://app.vitrus.ai).

## Actors

```python
import asyncio
from vitrus import Vitrus

async def main():
    vitrus = Vitrus(
        api_key="YOUR_API_KEY",
        world="YOUR_WORLD_ID"
    )

    # Create an actor
    actor = await vitrus.actor("forest", {
        "human": "Tom Hanks",
        "eyes": "green"
    })

    # Register a command handler
    def walk(args):
        print("received", args)
        return "run forest, run!"
    
    actor.on("walk", walk)
    
    # Keep the connection open
    # In a real application, you would have your main application logic here
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        print("Shutting down")
    
asyncio.run(main())
```

## Agents

On the Agent side, once connected to, the actor can be treated as "functions".

```python
import asyncio
from vitrus import Vitrus

async def main():
    vitrus = Vitrus(
        api_key="YOUR_API_KEY",
        world="YOUR_WORLD_ID"  # must match actor's world
    )

    # Get a handle to an actor
    actor = await vitrus.actor("forest")

    # Run a command on the actor
    resp = await actor.run("walk", {
        "direction": "front"
    })
    
    print(resp)
    
asyncio.run(main())
```

---

# How Vitrus works internally

Vitrus workflows, worlds, actors and agents runs on top of Distributed Actor Orchestration (DAO). A lightweight cloud system that enables the cross-communication of agents-world-actors. 