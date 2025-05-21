"""
Vitrus SDK for Python

A Python client for interfacing with the Vitrus WebSocket server.
Provides an Actor/Agent communication model with workflow orchestration.
"""

import asyncio
import inspect
import json
import logging
import random
import string
import uuid
import signal
import atexit
import threading
import weakref
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import websockets

# SDK version
SDK_VERSION = "0.1.0"
DEFAULT_BASE_URL = 'wss://vitrus-dao.onrender.com'

# Configure logging
logger = logging.getLogger("vitrus")

# Track all Vitrus instances for global cleanup
_vitrus_instances = weakref.WeakSet()

# Flag to track if signal handlers are set up
_signal_handlers_installed = False

# Function to handle cleanup on exit
def _global_cleanup():
    """Clean up all Vitrus instances on program exit"""
    loop = None
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        # Create a new event loop if none exists
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    if loop and not loop.is_closed():
        for instance in list(_vitrus_instances):
            if hasattr(instance, '_cleanup') and callable(instance._cleanup):
                try:
                    loop.run_until_complete(instance._cleanup())
                except Exception as e:
                    logger.debug(f"Error during Vitrus instance cleanup: {e}")

# Register cleanup with atexit
atexit.register(_global_cleanup)

# Signal handler for graceful shutdown
def _signal_handler(sig, frame):
    """Handle termination signals by cleaning up all Vitrus instances"""
    logger.debug(f"Received signal {sig}, initiating Vitrus SDK cleanup")
    _global_cleanup()
    
# Install signal handlers
def _install_signal_handlers():
    """Install signal handlers if not already done"""
    global _signal_handlers_installed
    if not _signal_handlers_installed:
        try:
            signal.signal(signal.SIGINT, _signal_handler)
            signal.signal(signal.SIGTERM, _signal_handler)
            _signal_handlers_installed = True
        except (ValueError, AttributeError):
            # This happens when not in the main thread
            pass

class Scene:
    """Scene class for managing scene objects"""

    def __init__(self, vitrus, scene_id: str):
        self.vitrus = vitrus
        self.scene_id = scene_id

    def set(self, structure: Any) -> None:
        """Set a structure to the scene"""
        # Implementation would update scene structure
        pass

    def add(self, obj: Any) -> None:
        """Add an object to the scene"""
        # Implementation would add object to scene
        pass

    def update(self, params: Dict[str, Any]) -> None:
        """Update an object in the scene"""
        # Implementation would update object in scene
        pass

    def remove(self, object_id: str) -> None:
        """Remove an object from the scene"""
        # Implementation would remove object from scene
        pass

    def get(self) -> Dict[str, Any]:
        """Get the scene"""
        # Implementation would fetch scene data
        return {"id": self.scene_id}


class Actor:
    """Actor/Player class"""

    def __init__(self, vitrus, name: str, metadata: Dict[str, Any] = None):
        self.vitrus = vitrus
        self.name = name
        self.metadata = metadata or {}
        self.command_handlers = {}
        
    def __del__(self):
        """Destructor to ensure cleanup"""
        try:
            # We can't use async in __del__, so just remove from tracking
            if hasattr(self, 'vitrus') and hasattr(self.vitrus, 'created_actors'):
                self.vitrus.created_actors.discard(self)
        except Exception:
            # Never raise exceptions in __del__
            pass

    def on(self, command_name: str, handler: Callable) -> 'Actor':
        """Register a command handler"""
        self.command_handlers[command_name] = handler
        
        # Extract parameter types
        parameter_types = self._get_parameter_types(handler)
        
        # Register with Vitrus (local handler map)
        self.vitrus.register_actor_command_handler(
            self.name, command_name, handler, parameter_types)
        
        # Register command with server *only if* currently connected as this actor
        if self.vitrus.is_authenticated and self.vitrus.actor_name == self.name:
            # For debugging
            if self.vitrus.debug:
                logger.debug(f"Registering command {command_name} with the server for actor {self.name}")
            
            # Create a task but don't await it to avoid blocking
            asyncio.create_task(self.vitrus.register_command(
                self.name, command_name, parameter_types))
        elif self.vitrus.debug:
            logger.debug(
                f"Not sending REGISTER_COMMAND for {command_name} on {self.name} as SDK is not authenticated as this actor.")
        
        return self

    async def run(self, command_name: str, *args) -> Any:
        """Run a command on an actor"""
        return await self.vitrus.run_command(self.name, command_name, args)

    def get_metadata(self) -> Dict[str, Any]:
        """Get actor metadata"""
        return self.metadata

    def update_metadata(self, new_metadata: Dict[str, Any]) -> None:
        """Update actor metadata"""
        self.metadata.update(new_metadata)
        # TODO: Send metadata update to server

    async def disconnect(self) -> None:
        """Disconnect the actor asynchronously if the SDK is currently connected as this actor."""
        # Call the SDK disconnection method for this actor
        self.vitrus.disconnect_if_actor(self.name)
        
        # If we need to do a full async disconnection with the SDK
        await self.vitrus.disconnect()

    def _get_parameter_types(self, func: Callable) -> List[str]:
        """Extract parameter types from function signature"""
        sig = inspect.signature(func)
        param_types = []

        for param in sig.parameters.values():
            # Skip 'self' parameter for methods
            if param.name == 'self':
                continue

            # Try to extract type information if available
            if param.annotation is not inspect.Parameter.empty:
                param_types.append(str(param.annotation.__name__))
            else:
                param_types.append('any')

        return param_types


class Vitrus:
    """Main Vitrus class"""

    def __init__(self, api_key: str, world: str = None, base_url: str = DEFAULT_BASE_URL, debug: bool = False):
        self.api_key = api_key
        self.world_id = world
        self.base_url = base_url
        self.debug = debug
        self.ws = None
        self.client_id = ""
        self.connected = False
        self.is_authenticated = False
        self.actor_name = None
        self.redis_channel = None
        self.message_handlers = {}
        self.pending_requests = {}
        self.actor_command_handlers = {}
        self.actor_command_signatures = {}
        self.actor_metadata = {}
        self.connection_task = None
        self.created_actors = set()  # Track all actors created by this instance

        if self.debug:
            logger.setLevel(logging.DEBUG)
            logger.debug(
                f"Vitrus v{SDK_VERSION} initializing with options: {{'apiKey': '***', 'world': {world}, 'baseUrl': {base_url}, 'debug': {debug}}}")

        # Add this instance to the global set for cleanup
        _vitrus_instances.add(self)
        
        # Install signal handlers
        _install_signal_handlers()

    async def connect(self, actor_name: str = None, metadata: Dict[str, Any] = None) -> None:
        """Connect to the WebSocket server with authentication"""
        if self.connection_task and not self.connection_task.done():
            await self.connection_task
            return

        self.actor_name = actor_name or self.actor_name
        if self.actor_name and metadata:
            self.actor_metadata[self.actor_name] = metadata

        self.connection_task = asyncio.create_task(
            self._establish_websocket_connection())
        await self.connection_task

    async def _establish_websocket_connection(self) -> None:
        """Establish WebSocket connection and handle authentication"""
        if self.debug:
            logger.debug(
                f"Attempting to connect to WebSocket server: {self.base_url}")
        
        url = self.base_url
        # Add query parameters
        url += f"?apiKey={self.api_key}"
        if self.world_id:
            url += f"&worldId={self.world_id}"
        
        try:
            self.ws = await websockets.connect(url)
            self.connected = True
            
            if self.debug:
                logger.debug("Connected to WebSocket server")
            
            # Send HANDSHAKE message
            handshake_msg = {
                "type": "HANDSHAKE",
                "apiKey": self.api_key,
                "worldId": self.world_id,
                "actorName": self.actor_name,
                "metadata": self.actor_metadata.get(self.actor_name) if self.actor_name else None
            }
            
            if self.debug:
                logger.debug(f"Sending HANDSHAKE message: {handshake_msg}")
            
            await self.send_message(handshake_msg)
            
            # We'll start the message handler in _wait_for_authentication after auth completes
            # Wait for authentication
            await self._wait_for_authentication()
            
        except Exception as e:
            self.connected = False
            error_msg = f"Connection failed: {str(e)}"
            if self.world_id:
                error_msg = f"Connection Failed: Unable to connect to world '{self.world_id}'. This world may not exist, or the API key may be invalid. Original: {str(e)}"
            
            logger.error(error_msg)
            if self.debug:
                logger.debug(f"WebSocket connection error: {e}")
            
            raise Exception(error_msg)

    async def _message_handler(self) -> None:
        """Handle incoming WebSocket messages"""
        try:
            async for message in self.ws:
                try:
                    data = json.loads(message)
                    if self.debug and data.get("type") != "HANDSHAKE_RESPONSE":
                        logger.debug(f"Received message: {data}")

                    await self._handle_message(data)
                except json.JSONDecodeError:
                    logger.error(f"Error parsing WebSocket message: {message}")
        except websockets.exceptions.ConnectionClosed as e:
            self.connected = False
            self.is_authenticated = False
            logger.error(f"WebSocket connection closed: {e}")

            # Reject any pending requests
            for request_id, (future, _) in self.pending_requests.items():
                if not future.done():
                    future.set_exception(Exception(f"Connection lost: {e}"))

    async def _wait_for_authentication(self) -> None:
        """Wait for authentication to complete"""
        if self.is_authenticated:
            return
        
        if self.debug:
            logger.debug("Waiting for authentication...")
        
        auth_future = asyncio.Future()
        
        async def handle_auth_response(message):
            if self.debug:
                logger.debug(f"Processing auth response: {message}")
                
            if message["type"] == "HANDSHAKE_RESPONSE":
                response = message
                if response["success"]:
                    self.client_id = response["clientId"]
                    self.redis_channel = response.get("redisChannel")
                    self.is_authenticated = True
                    
                    # If actor info was included, restore it
                    if response.get("actorInfo") and self.actor_name:
                        # Store the actor metadata
                        self.actor_metadata[self.actor_name] = response["actorInfo"]["metadata"]
                        
                        # Re-register existing commands if available
                        if response["actorInfo"].get("registeredCommands"):
                            if self.debug:
                                logger.debug(
                                    f"Restoring registered commands: {response['actorInfo']['registeredCommands']}")
                            
                            # Create a signature map if it doesn't exist
                            if self.actor_name not in self.actor_command_signatures:
                                self.actor_command_signatures[self.actor_name] = {
                                }
                            
                            # Restore command signatures
                            signatures = self.actor_command_signatures[self.actor_name]
                            for cmd in response["actorInfo"]["registeredCommands"]:
                                signatures[cmd["name"]] = cmd["parameterTypes"]
                
                    if self.debug:
                        logger.debug(
                            f"Authentication successful, clientId: {self.client_id}")
                
                    auth_future.set_result(True)
                else:
                    error_message = response.get(
                        "message", "Authentication failed")
                    # Check for specific error codes
                    if response.get("error_code") == "invalid_api_key":
                        error_message = "Authentication Failed: The provided API Key is invalid or expired."
                    elif response.get("error_code") == "world_not_found":
                        error_message = response.get(
                            "message") or "Connection Failed: The world specified in the connection URL was not found."
                    elif response.get("error_code") == "world_not_found_handshake":
                        error_message = response.get(
                            "message") or "Connection Failed: The world specified in the handshake message was not found."
                    elif "Actors require a worldId" in error_message:
                        error_message = "Connection Failed: An actor connection requires a valid World ID to be specified."
                    
                    if self.debug:
                        logger.debug(f"Authentication failed: {error_message}")
                    
                    auth_future.set_exception(Exception(error_message))
        
        # Set up a direct message handler for this specific authentication request
        async def message_listener():
            try:
                async for message in self.ws:
                    try:
                        data = json.loads(message)
                        if self.debug:
                            logger.debug(f"Auth listener received: {data}")
                        if data.get("type") == "HANDSHAKE_RESPONSE":
                            await handle_auth_response(data)
                            # Exit the loop once authentication is handled
                            if auth_future.done():
                                break
                    except json.JSONDecodeError:
                        logger.error(f"Error parsing WebSocket message during auth: {message}")
            except Exception as e:
                if not auth_future.done():
                    auth_future.set_exception(e)
        
        # Start listening for messages
        listener_task = asyncio.create_task(message_listener())
        
        try:
            # Wait for authentication to complete
            await auth_future
        finally:
            # Cancel the listener task if it's still running
            if not listener_task.done():
                listener_task.cancel()
                try:
                    await listener_task
                except asyncio.CancelledError:
                    pass
            
            # Start the regular message handler if it's not running yet
            if self.is_authenticated:
                asyncio.create_task(self._message_handler())

    async def send_message(self, message: Dict[str, Any]) -> None:
        """Send a message to the WebSocket server"""
        if not self.connected:
            await self.connect()
            
        if self.ws and self.connected:
            if self.debug:
                logger.debug(
                    f"Sending message: {message}")
                
            await self.ws.send(json.dumps(message))
        else:
            if self.debug:
                logger.debug(
                    "Failed to send message - WebSocket not connected")
                
            raise Exception("WebSocket is not connected")

    async def _handle_message(self, message: Dict[str, Any]) -> None:
        """Handle incoming WebSocket messages"""
        message_type = message.get("type")

        # Handle handshake response
        if message_type == "HANDSHAKE_RESPONSE":
            # This is handled in _wait_for_authentication
            pass

        # Handle command from another client
        elif message_type == "COMMAND":
            if self.debug:
                logger.debug(f"Received command: {message}")

            await self._handle_command(message)

        # Handle response from actor
        elif message_type == "RESPONSE":
            request_id = message.get("requestId")
            result = message.get("result")
            error = message.get("error")

            if self.debug:
                logger.debug(
                    f"Received response for requestId: {request_id}, result: {result}, error: {error}")

            if request_id in self.pending_requests:
                future, _ = self.pending_requests.pop(request_id)
                if error:
                    future.set_exception(Exception(error))
                else:
                    future.set_result(result)

        # Handle workflow results
        elif message_type == "WORKFLOW_RESULT":
            request_id = message.get("requestId")
            result = message.get("result")
            error = message.get("error")

            if self.debug:
                logger.debug(
                    f"Received workflow result for requestId: {request_id}, result: {result}, error: {error}")

            if request_id in self.pending_requests:
                future, _ = self.pending_requests.pop(request_id)
                if error:
                    future.set_exception(Exception(error))
                else:
                    future.set_result(result)

        # Handle workflow list response
        elif message_type == "WORKFLOW_LIST":
            request_id = message.get("requestId")
            workflows = message.get("workflows")
            error = message.get("error")

            if self.debug:
                logger.debug(
                    f"Received workflow list for requestId: {request_id}, workflows: {workflows}, error: {error}")

            if request_id in self.pending_requests:
                future, _ = self.pending_requests.pop(request_id)
                if error:
                    future.set_exception(Exception(error))
                else:
                    future.set_result(workflows or [])

        # Handle custom message types
        elif message_type in self.message_handlers:
            for handler in self.message_handlers[message_type]:
                handler(message)

    async def _handle_command(self, message: Dict[str, Any]) -> None:
        """Handle incoming command message"""
        command_name = message.get("commandName")
        args = message.get("args", [])
        request_id = message.get("requestId")
        target_actor_name = message.get("targetActorName")
        source_channel = message.get("sourceChannel")
        
        if self.debug:
            logger.debug(
                f"Handling command: {command_name} for actor {target_actor_name}, requestId: {request_id}, args: {args}")
            logger.debug(f"Known actors: {list(self.actor_command_handlers.keys())}")
            if target_actor_name in self.actor_command_handlers:
                logger.debug(f"Commands for {target_actor_name}: {list(self.actor_command_handlers[target_actor_name].keys())}")
        
        if target_actor_name in self.actor_command_handlers:
            actor_handlers = self.actor_command_handlers[target_actor_name]
            
            # Try different variations of the command name
            possible_command_names = [
                command_name,
                command_name.replace('-', ''),  # No hyphens
                command_name.replace('-', '_'),  # Replace hyphens with underscores
                command_name.lower(),  # Lowercase
                command_name.upper()   # Uppercase
            ]
            
            handler = None
            used_command_name = None
            
            # Try to find a handler with any of the possible command name variations
            for cmd_name in possible_command_names:
                if cmd_name in actor_handlers:
                    handler = actor_handlers[cmd_name]
                    used_command_name = cmd_name
                    break
                
            if handler:
                if self.debug:
                    logger.debug(f"Found handler for command: {command_name} (matched as {used_command_name})")
                    
                try:
                    # Execute handler with unpacked arguments
                    if self.debug:
                        logger.debug(f"Executing handler with args type: {type(args).__name__}, value: {args}")
                        
                    if isinstance(args, list):
                        result = handler(*args)
                    elif isinstance(args, dict):
                        result = handler(**args)
                    else:
                        result = handler(args)
                        
                    # Handle coroutines
                    if inspect.iscoroutine(result):
                        if self.debug:
                            logger.debug(f"Handler returned coroutine, awaiting result")
                        result = await result
                        
                    if self.debug:
                        logger.debug(
                            f"Command executed successfully: {command_name}, result: {result}")
                        
                    await self.send_response({
                        "type": "RESPONSE",
                        "targetChannel": source_channel or "",
                        "requestId": request_id,
                        "result": result
                    })
                except Exception as e:
                    if self.debug:
                        logger.debug(
                            f"Command execution failed: {command_name}, error: {str(e)}")
                        logger.debug(f"Exception details: {e}", exc_info=True)
                        
                    await self.send_response({
                        "type": "RESPONSE",
                        "targetChannel": source_channel or "",
                        "requestId": request_id,
                        "error": str(e)
                    })
            elif self.debug:
                logger.debug(f"No handler found for command: {command_name}. Available commands: {list(actor_handlers.keys())}")
        elif self.debug:
            logger.debug(f"No actor found with name: {target_actor_name}. Known actors: {list(self.actor_command_handlers.keys())}")

    async def send_response(self, response: Dict[str, Any]) -> None:
        """Send a response message"""
        if self.debug:
            logger.debug(f"Sending response: {response}")

        await self.send_message(response)

    async def register_command(self, actor_name: str, command_name: str, parameter_types: List[str]) -> None:
        """Register a command with the server"""
        if self.debug:
            logger.debug(
                f"Registering command with server: actor={actor_name}, command={command_name}, params={parameter_types}")

        message = {
            "type": "REGISTER_COMMAND",
            "actorName": actor_name,
            "commandName": command_name,
            "parameterTypes": parameter_types
        }

        await self.send_message(message)

    def _generate_request_id(self) -> str:
        """Generate a unique request ID"""
        request_id = ''.join(random.choices(
            string.ascii_lowercase + string.digits, k=10))
        if self.debug:
            logger.debug(f"Generated requestId: {request_id}")

        return request_id

    async def authenticate(self, actor_name: str = None, metadata: Dict[str, Any] = None) -> bool:
        """Authenticate with the API"""
        if self.debug:
            logger.debug(f"Initiating connection sequence..." +
                         (f" (intended actor: {actor_name})" if actor_name else ""))

        # Require worldId if intending to be an actor
        if actor_name and not self.world_id:
            raise Exception(
                "Vitrus SDK requires a worldId to authenticate as an actor.")

        # Store actor name and metadata for use in connection
        self.actor_name = actor_name
        if actor_name and metadata:
            self.actor_metadata[actor_name] = metadata

        # Connect or reconnect
        await self.connect(actor_name, metadata)

        # If connected as an actor, register any pending commands
        if self.is_authenticated and actor_name:
            if self.debug:
                logger.debug(f"Successfully authenticated as {actor_name}, registering pending commands")
            await self._register_pending_commands(actor_name)
            if self.debug:
                if actor_name in self.actor_command_handlers:
                    logger.debug(f"Commands registered for {actor_name}: {list(self.actor_command_handlers[actor_name].keys())}")
                else:
                    logger.debug(f"No commands found for {actor_name}")

        return self.is_authenticated

    def register_actor_command_handler(self, actor_name: str, command_name: str, handler: Callable, parameter_types: List[str] = None) -> None:
        """Register a command handler for an actor"""
        if self.debug:
            logger.debug(
                f"Registering command handler: actor={actor_name}, command={command_name}, params={parameter_types}")

        # Store the command handler
        if actor_name not in self.actor_command_handlers:
            self.actor_command_handlers[actor_name] = {}
        self.actor_command_handlers[actor_name][command_name] = handler

        # Store the parameter types
        if actor_name not in self.actor_command_signatures:
            self.actor_command_signatures[actor_name] = {}
        self.actor_command_signatures[actor_name][command_name] = parameter_types or [
        ]

    async def actor(self, name: str, options: Dict[str, Any] = None) -> Actor:
        """Create or get an actor"""
        if self.debug:
            logger.debug(
                f"Creating/getting actor handle: {name}, options={options}")

        # Require worldId to create/authenticate as an actor if options are provided
        if options is not None and not self.world_id:
            raise Exception(
                "Vitrus SDK requires a worldId to create/authenticate as an actor.")

        # Store actor metadata immediately if provided
        if options is not None:
            self.actor_metadata[name] = options

        actor = Actor(self, name, options if options is not None else {})
        
        # Track this actor for cleanup
        self.created_actors.add(actor)

        # If options are provided, it implies intent to *be* this actor
        if options is not None and (not self.is_authenticated or self.actor_name != name):
            if self.debug:
                logger.debug(
                    f"Options provided for actor {name}, ensuring authentication as this actor...")

            try:
                await self.authenticate(name, options)
                if self.debug:
                    logger.debug(
                        f"Successfully authenticated as actor {name}.")

                # After successful auth, ensure any commands queued via .on() are registered
                await self._register_pending_commands(name)
            except Exception as e:
                logger.error(f"Failed to auto-authenticate actor {name}: {e}")
                raise

        return actor

    def scene(self, scene_id: str) -> Scene:
        """Get a scene"""
        if self.debug:
            logger.debug(f"Getting scene: {scene_id}")

        return Scene(self, scene_id)

    async def run_command(self, actor_name: str, command_name: str, args: List[Any]) -> Any:
        """Run a command on an actor"""
        if self.debug:
            logger.debug(
                f"Running command: actor={actor_name}, command={command_name}, args={args}")

        # Require worldId to run commands
        if not self.world_id:
            raise Exception(
                "Vitrus SDK requires a worldId to run commands on actors.")

        # If not authenticated yet, auto-authenticate
        if not self.is_authenticated:
            await self.authenticate()

        request_id = self._generate_request_id()
        future = asyncio.Future()

        self.pending_requests[request_id] = (future, None)

        command = {
            "type": "COMMAND",
            "targetActorName": actor_name,
            "commandName": command_name,
            "args": args,
            "requestId": request_id
        }

        try:
            await self.send_message(command)
        except Exception as e:
            if self.debug:
                logger.debug(f"Failed to send command: {e}")

            del self.pending_requests[request_id]
            raise

        return await future

    async def workflow(self, workflow_name: str, args: Dict[str, Any] = None) -> Any:
        """Run a workflow"""
        if self.debug:
            logger.debug(f"Running workflow: {workflow_name}, args={args}")

        # Automatically authenticate if not authenticated yet
        if not self.is_authenticated:
            await self.authenticate()

        request_id = self._generate_request_id()
        future = asyncio.Future()

        self.pending_requests[request_id] = (future, None)

        workflow_message = {
            "type": "WORKFLOW",
            "workflowName": workflow_name,
            "args": args or {},
            "requestId": request_id
        }

        try:
            await self.send_message(workflow_message)
        except Exception as e:
            if self.debug:
                logger.debug(f"Failed to send workflow: {e}")

            del self.pending_requests[request_id]
            raise

        return await future

    async def upload_image(self, image: Any, filename: str = "image") -> str:
        """Upload an image"""
        if self.debug:
            logger.debug(f"Uploading image: {filename}")

        # Implementation would handle image uploads
        # For now, just return a mock URL
        return f"https://vitrus.io/images/{filename}"

    async def add_record(self, data: Dict[str, Any], name: str = None) -> str:
        """Add a record"""
        if self.debug:
            logger.debug(f"Adding record: data={data}, name={name}")

        # Implementation would store the record
        # For now, just return success
        return name or self._generate_request_id()

    async def list_workflows(self) -> List[Dict[str, Any]]:
        """List available workflows on the server"""
        if self.debug:
            logger.debug("Requesting workflow list with definitions...")

        # Automatically authenticate if not authenticated yet
        if not self.is_authenticated:
            await self.authenticate()

        request_id = self._generate_request_id()
        future = asyncio.Future()

        self.pending_requests[request_id] = (future, None)

        message = {
            "type": "LIST_WORKFLOWS",
            "requestId": request_id
        }

        try:
            await self.send_message(message)
        except Exception as e:
            if self.debug:
                logger.debug(f"Failed to send LIST_WORKFLOWS message: {e}")

            del self.pending_requests[request_id]
            raise

        return await future

    async def _register_pending_commands(self, actor_name: str) -> None:
        """Register commands that might have been added via actor.on() before authentication"""
        if actor_name not in self.actor_command_handlers or actor_name not in self.actor_command_signatures:
            if self.debug:
                logger.debug(f"No pending commands to register for actor {actor_name}")
            return

        handlers = self.actor_command_handlers[actor_name]
        signatures = self.actor_command_signatures[actor_name]

        if self.debug:
            logger.debug(
                f"Registering pending commands for actor {actor_name}: {list(handlers.keys())}")

        for command_name, parameter_types in signatures.items():
            if command_name in handlers:  # Ensure handler still exists
                try:
                    if self.debug:
                        logger.debug(f"Registering command {command_name} with server")
                    await self.register_command(actor_name, command_name, parameter_types)
                except Exception as e:
                    logger.error(
                        f"Error registering pending command {command_name} for actor {actor_name}: {e}")
                
        # Double-check registration was successful
        if self.debug:
            logger.debug(f"Command registration complete. Registered commands for {actor_name}: {list(handlers.keys())}")

    def disconnect_if_actor(self, actor_name: str) -> None:
        """Disconnects the WebSocket if the SDK is currently authenticated as the specified actor.
        Also unregisters any commands for this actor."""
        if self.actor_name == actor_name and self.is_authenticated and self.ws and self.connected:
            if self.debug:
                logger.debug(
                    f"Actor '{actor_name}' is disconnecting and unregistering commands.")
            
            # Unregister commands from server (if needed by your server implementation)
            try:
                # Clear local command references
                if actor_name in self.actor_command_handlers:
                    self.actor_command_handlers.pop(actor_name, None)
                
                if actor_name in self.actor_command_signatures:
                    self.actor_command_signatures.pop(actor_name, None)
                
                if actor_name in self.actor_metadata:
                    self.actor_metadata.pop(actor_name, None)
                    
                # Close the connection - this will trigger the websocket close handler
                asyncio.create_task(self.ws.close())
                # The message handler will manage further state changes
            except Exception as e:
                if self.debug:
                    logger.debug(f"Error during actor disconnection: {e}")
        elif self.debug:
            if self.actor_name != actor_name:
                logger.debug(
                    f"disconnectIfActor: SDK not connected as '{actor_name}' (currently: {self.actor_name or 'agent/none'}). No action taken.")
            elif not self.is_authenticated:
                logger.debug(
                    f"disconnectIfActor: SDK not authenticated as '{actor_name}'. No action taken.")
            else:
                logger.debug(
                    f"disconnectIfActor: WebSocket for '{actor_name}' not open or available. No action taken.")

    async def disconnect(self) -> None:
        """Disconnect from the WebSocket server and clean up all resources"""
        if self.debug:
            logger.debug("Disconnecting from Vitrus server and cleaning resources")
        
        # Disconnect if connected as an actor
        if self.actor_name and self.is_authenticated:
            self.disconnect_if_actor(self.actor_name)
        # Otherwise just close the connection if it's open
        elif self.connected and self.ws:
            try:
                await self.ws.close()
            except Exception as e:
                if self.debug:
                    logger.debug(f"Error during WebSocket disconnection: {e}")
        
        # Clean up resources
        self.connected = False
        self.is_authenticated = False
        self.client_id = ""
        self.redis_channel = None
        
        # Clear any pending requests
        for request_id, (future, _) in list(self.pending_requests.items()):
            if not future.done():
                future.set_exception(Exception("Disconnected by user"))
        self.pending_requests.clear()
        
        if self.debug:
            logger.debug("Disconnection complete")

    async def _cleanup(self) -> None:
        """Clean up resources when the SDK is being destroyed"""
        if self.debug:
            logger.debug("Cleaning up Vitrus resources")
        
        # Clean up all created actors
        if self.created_actors:
            if self.debug:
                logger.debug(f"Cleaning up {len(self.created_actors)} actors")
            
            # Make a copy since the set might change during iteration
            for actor in list(self.created_actors):
                try:
                    if self.debug:
                        logger.debug(f"Disconnecting actor: {actor.name}")
                    if self.actor_name == actor.name and self.is_authenticated:
                        self.disconnect_if_actor(actor.name)
                except Exception as e:
                    if self.debug:
                        logger.debug(f"Error during actor cleanup: {e}")
        
        # Clear the actor set
        self.created_actors.clear()
        
        # Disconnect from the server
        await self.disconnect()
        
        # Remove this instance from the global set
        _vitrus_instances.discard(self)
        
        if self.debug:
            logger.debug("Vitrus resources cleaned up")
