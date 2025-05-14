"""
Vitrus SDK for Python

A Python client for interfacing with the Vitrus WebSocket server.
Provides an Actor/Agent communication model with workflow orchestration.
"""

import asyncio
import json
import logging
import uuid
import websockets
import inspect # For parameter inspection
from typing import Any, Dict, List, Optional, Callable, Coroutine, Union

# Configure logging
logger = logging.getLogger(__name__)
# logging.basicConfig(level=logging.INFO) # Or use debug for more verbosity

SDK_VERSION = "0.1.0"  # Needs to be kept in sync with setup.py for now
DEFAULT_BASE_URL = "wss://vitrus-dao.onrender.com"

# --- Message Type Definitions (mirroring TypeScript) ---
# These can be expanded into dataclasses or TypedDicts for better type safety if desired.

class Actor:
    """Represents an Actor in the Vitrus ecosystem."""
    def __init__(self, vitrus_instance: 'Vitrus', name: str, metadata: Optional[Dict[str, Any]] = None):
        self.vitrus = vitrus_instance
        self.name = name
        self.metadata = metadata if metadata is not None else {}
        self._command_handlers: Dict[str, Callable[..., Coroutine[Any, Any, Any]]] = {}
        logger.info(f"Actor instance '{self.name}' created locally.")

    def on(self, command_name: str, handler: Callable[..., Coroutine[Any, Any, Any]]):
        """Register an asynchronous command handler for this actor."""
        if not asyncio.iscoroutinefunction(handler):
            raise ValueError("Handler must be an async function (coroutine).")
        
        self._command_handlers[command_name] = handler
        parameter_types = [] # In Python, runtime type introspection is different
        try:
            sig = inspect.signature(handler)
            # We can log parameter names, but types are not as straightforward as in TS compile-time
            # For now, sending an empty list or just names might be sufficient if server doesn't strictly use types
            parameter_types = [p.name for p in sig.parameters.values()]
        except Exception as e:
            logger.warning(f"Could not inspect parameters for handler '{command_name}': {e}")

        self.vitrus.register_actor_command_handler(
            actor_name=self.name, 
            command_name=command_name, 
            handler=handler, 
            parameter_names=parameter_types # Sending names for now
        )
        logger.info(f"Registered handler for command '{command_name}' on actor '{self.name}'.")

    async def run(self, command_name: str, args: Optional[Union[Dict[str, Any], List[Any]]] = None) -> Any:
        """Run a command on this actor (if it's a remote actor) or a different actor."""
        # This method assumes `self.name` is the *target* actor.
        # This aligns with `actor_proxy.run("some_command", ...)` from the agent side.
        return await self.vitrus.run_command(target_actor_name=self.name, command_name=command_name, args=args if args is not None else {})

    def get_metadata(self) -> Dict[str, Any]:
        return self.metadata

    async def update_metadata(self, new_metadata: Dict[str, Any]):
        self.metadata.update(new_metadata)
        # If the SDK is connected as this actor, send an update
        if self.vitrus.actor_name == self.name and self.vitrus.is_authenticated:
            await self.vitrus._send_metadata_update(self.name, self.metadata)
        else:
            logger.debug(f"Metadata for actor '{self.name}' updated locally. SDK not connected as this actor, or not authenticated.")

    async def disconnect(self):
        """If the SDK is currently connected as this specific actor, disconnect it."""
        await self.vitrus.disconnect_if_actor(self.name)

class Vitrus:
    """Main client for interacting with the Vitrus platform."""

    def __init__(
        self,
        api_key: str,
        world_id: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        debug: bool = False,
        # actor_name: Optional[str] = None, # actor_name is now primarily handled during connect()
    ):
        if not api_key:
            raise ValueError("API key is required.")

        self.api_key = api_key
        self.world_id = world_id
        self.base_url = base_url
        self.debug = debug
        self.actor_name: Optional[str] = None # Name of the actor this client is authenticated as, if any.

        self._ws: Optional[websockets.client.WebSocketClientProtocol] = None
        self._client_id: str = ""
        self._is_connected: bool = False
        self._is_authenticated: bool = False # Server-side handshake successful
        self._redis_channel: Optional[str] = None

        self._pending_requests: Dict[str, asyncio.Future] = {}
        # Stores handlers for commands *this* client instance will execute if it *is* the named actor
        self._actor_command_handlers: Dict[str, Dict[str, Callable[..., Coroutine[Any, Any, Any]]]] = {}
        # Stores parameter signatures for commands this actor can handle (used for REGISTER_COMMAND)
        self._actor_command_signatures: Dict[str, Dict[str, List[str]]] = {}
        # Stores metadata for actors this client is *acting as*
        self._actor_metadata_cache: Dict[str, Any] = {}

        self._connection_lock = asyncio.Lock()
        self._listen_task: Optional[asyncio.Task] = None
        self._keep_alive_task: Optional[asyncio.Task] = None

        if self.debug:
            logger.setLevel(logging.DEBUG)
        else:
            logger.setLevel(logging.INFO)
        
        logger.info(f"Vitrus SDK v{SDK_VERSION} initialized. Base URL: {self.base_url}")

    def _generate_request_id(self) -> str:
        return str(uuid.uuid4())

    async def connect(self, actor_name: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Establishes a WebSocket connection and performs handshake/authentication."""
        async with self._connection_lock: # Ensure only one connection attempt at a time
            if self._is_connected and self._ws and self._ws.open:
                # If already connected, and trying to connect as a different actor, or re-auth
                if actor_name and self.actor_name != actor_name:
                    logger.info(f"Switching actor context from {self.actor_name} to {actor_name}. Re-authenticating.")
                    # Potentially disconnect old actor session cleanly if server requires
                    self._is_authenticated = False # Reset auth status for new handshake
                    self.actor_name = actor_name
                    if metadata is not None:
                        self._actor_metadata_cache[actor_name] = metadata
                    await self._authenticate() # Re-authenticate with new actor_name
                    return
                elif not actor_name and self.actor_name: # Connecting as agent after being actor
                     logger.info(f"Switching from actor '{self.actor_name}' to agent context. Re-authenticating.")
                     self._is_authenticated = False
                     self.actor_name = None
                     await self._authenticate()
                     return
                logger.info("Already connected and authenticated.")
                return

            try:
                logger.info(f"Connecting to WebSocket server at {self.base_url}...")
                self._ws = await websockets.connect(self.base_url, subprotocols=["vitrus-protocol"])
                self._is_connected = True
                logger.info("WebSocket connection established.")
                
                self.actor_name = actor_name # Set actor_name for this connection attempt
                if actor_name and metadata is not None:
                     self._actor_metadata_cache[actor_name] = metadata

                await self._authenticate()

                if not self._listen_task or self._listen_task.done():
                    self._listen_task = asyncio.create_task(self._listen_for_messages())
                if not self._keep_alive_task or self._keep_alive_task.done():
                    self._keep_alive_task = asyncio.create_task(self._send_ping_periodically()) # Optional

            except (websockets.exceptions.WebSocketException, ConnectionRefusedError, OSError) as e:
                self._is_connected = False
                self._is_authenticated = False
                logger.error(f"WebSocket connection failed: {e}")
                raise # Re-raise the exception to the caller
    
    async def _authenticate(self) -> None:
        """Sends handshake message and waits for response."""
        if not self._ws or not self._is_connected:
            raise ConnectionError("WebSocket not connected. Call connect() first.")

        handshake_payload: Dict[str, Any] = {
            "type": "HANDSHAKE",
            "apiKey": self.api_key,
            "sdkVersion": SDK_VERSION,
            "sdkType": "python"
        }
        if self.world_id:
            handshake_payload["worldId"] = self.world_id
        if self.actor_name:
            handshake_payload["actorName"] = self.actor_name
            # Use cached metadata if available for this actor_name
            if self.actor_name in self._actor_metadata_cache:
                 handshake_payload["metadata"] = self._actor_metadata_cache[self.actor_name]
            elif self.actor_name in self._actor_command_handlers: # Fallback to current instance if metadata not pre-set during connect
                 # This logic might need refinement: should metadata primarily come from actor() or connect()?
                 # For now, if actor() was called first and set metadata, it might not be here unless connect() also passed it.
                 # The TS SDK seems to pass it during the Vitrus constructor or actor() then it's used in connect()
                 pass # Metadata is now more explicitly passed via connect() or actor()

        logger.debug(f"Sending HANDSHAKE: {handshake_payload}")
        await self._send_message(handshake_payload) 
        # Authentication response is handled by _handle_message, which sets _is_authenticated
        # We need a way to wait for this specific handshake response.
        auth_future = asyncio.get_event_loop().create_future()
        self._pending_requests["HANDSHAKE_RESPONSE"] = auth_future # Use a special key
        
        try:
            await asyncio.wait_for(auth_future, timeout=10.0) # Wait for 10 seconds
            if not self._is_authenticated:
                 # The future was resolved, but auth failed (handled in _handle_message)
                 # The error message would have been set by _handle_message on the future
                 error_msg = auth_future.result().get("message", "Authentication failed due to unknown server error.") if isinstance(auth_future.result(), dict) else "Authentication failed."
                 raise ConnectionAbortedError(f"Authentication rejected by server: {error_msg}")
            logger.info(f"Authentication successful. Client ID: {self._client_id}" + (f", Actor: {self.actor_name}" if self.actor_name else ""))
            if self.actor_name:
                await self._register_pending_commands(self.actor_name) # Register commands if connected as actor

        except asyncio.TimeoutError:
            self._is_authenticated = False
            logger.error("Authentication timed out.")
            # Clean up future
            if "HANDSHAKE_RESPONSE" in self._pending_requests:
                self._pending_requests["HANDSHAKE_RESPONSE"].set_exception(ConnectionRefusedError("Authentication timed out"))
                del self._pending_requests["HANDSHAKE_RESPONSE"]
            raise ConnectionRefusedError("Authentication timed out waiting for server response.")
        finally:
             # Clean up future if it's still there and not handled by timeout
            if "HANDSHAKE_RESPONSE" in self._pending_requests and not self._pending_requests["HANDSHAKE_RESPONSE"].done():
                # Should not happen if timeout or success occurred, but as a safeguard
                del self._pending_requests["HANDSHAKE_RESPONSE"]

    async def _send_ping_periodically(self, interval: int = 30):
        """Sends a ping to the server periodically to keep the connection alive."""
        while self._is_connected and self._ws and self._ws.open:
            try:
                await self._ws.ping()
                logger.debug("Ping sent.")
                await asyncio.sleep(interval)
            except websockets.exceptions.ConnectionClosed:
                logger.info("Connection closed, stopping pings.")
                break
            except Exception as e:
                logger.error(f"Error sending ping: {e}")
                # Potentially try to reconnect or mark as disconnected
                await self.disconnect(expected=False) # Mark as disconnected
                break
    
    async def _listen_for_messages(self) -> None:
        """Continuously listens for messages from the WebSocket server."""
        if not self._ws:
            logger.error("Cannot listen, WebSocket is not initialized.")
            return
        try:
            async for message_str in self._ws:
                if self.debug:
                    logger.debug(f"Received raw message: {message_str}")
                try:
                    message = json.loads(message_str)
                    await self._handle_message(message)
                except json.JSONDecodeError:
                    logger.error(f"Failed to decode JSON message: {message_str}")
                except Exception as e:
                    logger.error(f"Error handling message: {e} - Message: {message_str}")
        except websockets.exceptions.ConnectionClosedOK:
            logger.info("WebSocket connection closed gracefully.")
        except websockets.exceptions.ConnectionClosedError as e:
            logger.error(f"WebSocket connection closed with error: {e}")
        except Exception as e:
            logger.error(f"Exception in message listener: {e}")
        finally:
            logger.info("Message listener loop ended.")
            self._is_connected = False
            self._is_authenticated = False
            # Cancel pending requests
            for req_id, future in self._pending_requests.items():
                if not future.done():
                    future.set_exception(ConnectionError("Connection lost"))
            self._pending_requests.clear()

    async def _send_message(self, payload: Dict[str, Any]) -> None:
        """Sends a JSON payload to the WebSocket server."""
        if not self._ws or not self._ws.open:
            # Try to reconnect if not intentionally disconnecting
            # This could be made more sophisticated with backoff, etc.
            logger.warning("WebSocket not open. Attempting to reconnect...")
            try:
                await self.connect(actor_name=self.actor_name) # Try to restore connection with current context
                if not self._ws or not self._ws.open: # Check again after connect attempt
                     raise ConnectionError("WebSocket is not connected. Call connect() first.")
            except Exception as e:
                logger.error(f"Failed to reconnect automatically: {e}")
                raise ConnectionError("WebSocket is not connected and auto-reconnect failed.")

        try:
            message_str = json.dumps(payload)
            if self.debug:
                logger.debug(f"Sending message: {message_str}")
            await self._ws.send(message_str)
        except websockets.exceptions.ConnectionClosed as e:
            logger.error(f"Failed to send message, connection closed: {e}")
            self._is_connected = False
            self._is_authenticated = False
            raise
        except Exception as e:
            logger.error(f"Error sending message: {e}")
            raise

    async def _handle_message(self, message: Dict[str, Any]) -> None:
        """Handles incoming messages based on their type."""
        msg_type = message.get("type")
        if self.debug:
            logger.debug(f"Handling message of type: {msg_type}, content: {message}")

        if msg_type == "HANDSHAKE_RESPONSE":
            future = self._pending_requests.pop("HANDSHAKE_RESPONSE", None)
            if future and not future.done():
                if message.get("success"):
                    self._client_id = message.get("clientId", "")
                    self._redis_channel = message.get("redisChannel")
                    self._is_authenticated = True
                    logger.info(f"Handshake successful. Client ID: {self._client_id}, Actor: {self.actor_name or 'Agent'}")
                    future.set_result(message) # Resolve with the message content for inspection if needed
                else:
                    error_msg = message.get("message", "Handshake failed.")
                    error_code = message.get("error_code", "UnknownError")
                    logger.error(f"Handshake failed: {error_msg} (Code: {error_code})")
                    self._is_authenticated = False
                    future.set_result(message) # Still resolve, but indicates failure
                    # No, set exception to propagate the error for connect() to catch
                    # future.set_exception(ConnectionAbortedError(f"Handshake failed: {error_msg}"))
                    # The connect method will check _is_authenticated after future resolves
            else:
                logger.warning("Received HANDSHAKE_RESPONSE but no pending request or future already done.")
        
        elif msg_type == "RESPONSE" or msg_type == "WORKFLOW_RESULT" or msg_type == "WORKFLOW_LIST":
            request_id = message.get("requestId")
            if request_id and request_id in self._pending_requests:
                future = self._pending_requests.pop(request_id)
                if not future.done():
                    if "error" in message and message["error"] is not None:
                        logger.error(f"Received error for request {request_id}: {message['error']}")
                        future.set_exception(RuntimeError(str(message["error"])))
                    else:
                        result_key = "result" if msg_type == "WORKFLOW_RESULT" else "workflows" if msg_type == "WORKFLOW_LIST" else "result"
                        future.set_result(message.get(result_key))
            else:
                logger.warning(f"Received {msg_type} for unknown or completed request ID: {request_id}")

        elif msg_type == "COMMAND":
            await self._handle_command_message(message)
        
        elif msg_type == "ERROR": # Generic server error message
            error_message = message.get("message", "Unknown server error")
            request_id = message.get("requestId") # Some errors might be tied to a request
            if request_id and request_id in self._pending_requests:
                future = self._pending_requests.pop(request_id)
                if not future.done():
                    future.set_exception(RuntimeError(f"Server error: {error_message}"))
            else:
                logger.error(f"Received unassociated server error: {error_message}")
        
        else:
            logger.warning(f"Received unhandled message type: {msg_type}")

    async def _handle_command_message(self, message: Dict[str, Any]):
        """Handles an incoming COMMAND message if this client is the target actor."""
        target_actor_name = message.get("targetActorName") # Server should fill this based on world routing
        command_name = message.get("commandName")
        args = message.get("args", {})
        request_id = message.get("requestId")
        source_channel = message.get("sourceChannel") # Who to reply to

        response_payload: Dict[str, Any] = {
            "type": "RESPONSE",
            "requestId": request_id,
            "targetChannel": source_channel, # Echo back to sender via server routing
        }

        # Check if this instance is the intended actor and has a handler
        if self.actor_name == target_actor_name and command_name in self._actor_command_handlers.get(target_actor_name, {}):
            handler = self._actor_command_handlers[target_actor_name][command_name]
            logger.info(f"Actor '{target_actor_name}' executing command '{command_name}' with args: {args}")
            try:
                # Ensure args are passed correctly based on how they arrive (dict or list)
                # The TS SDK passes args as an array to actor.run, then spreads it.
                # Here, `message.get("args")` is likely a dict or list from JSON.
                # Handlers should be flexible or expect a certain structure.
                # For simplicity, if args is a list, spread it. If a dict, pass as kwargs or single arg.
                
                # Inspect handler to decide how to pass arguments
                sig = inspect.signature(handler)
                handler_params = list(sig.parameters.keys())

                if isinstance(args, dict):
                    # If args is a dict, try to match to parameter names
                    # This is more robust if the server sends named arguments
                    final_args = {k: args[k] for k in args if k in handler_params}
                    missing_params = [p for p in handler_params if p not in args and sig.parameters[p].default == inspect.Parameter.empty]
                    if missing_params:
                        raise TypeError(f"Missing required arguments for {command_name}: {missing_params}")
                    result = await handler(**final_args)
                elif isinstance(args, list):
                     # If args is a list, pass as positional arguments
                    if len(args) > len(handler_params) and not any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values()):
                        raise TypeError(f"Too many positional arguments for {command_name}")
                    result = await handler(*args)
                else: # Default to passing it as the first argument if it's a single value or not dict/list
                    result = await handler(args)
                
                response_payload["result"] = result
            except Exception as e:
                logger.error(f"Error executing command '{command_name}' on actor '{target_actor_name}': {e}", exc_info=True)
                response_payload["error"] = str(e)
        else:
            error_msg = f"Actor '{target_actor_name}' not handled by this client or command '{command_name}' not registered."
            logger.warning(error_msg)
            response_payload["error"] = error_msg
        
        await self._send_message(response_payload)

    async def disconnect(self, expected: bool = True) -> None:
        """Closes the WebSocket connection."""
        if self._ws and self._is_connected:
            logger.info("Disconnecting WebSocket...")
            try:
                await self._ws.close(code=1000 if expected else 1001)
            except Exception as e:
                logger.error(f"Error during WebSocket close: {e}")
            finally:
                self._ws = None
                self._is_connected = False
                self._is_authenticated = False
                self.actor_name = None # Clear actor context on disconnect
                self._client_id = ""
                self._redis_channel = None
                if self._listen_task and not self._listen_task.done():
                    self._listen_task.cancel()
                if self._keep_alive_task and not self._keep_alive_task.done():
                    self._keep_alive_task.cancel()
                logger.info("WebSocket disconnected.")
        else:
            logger.info("WebSocket already disconnected or not initialized.")
        
        # Clear handlers and metadata related to any actor this instance might have been
        # self._actor_command_handlers.clear() # Or only if self.actor_name was set?
        # self._actor_command_signatures.clear()
        # self._actor_metadata_cache.clear()
        # This might be too aggressive if the Vitrus instance is meant to be reconnected.
        # For now, let's clear actor_name, which is the primary indicator of acting status.

    async def disconnect_if_actor(self, actor_name_to_check: str):
        """Disconnects the client if it's currently connected as the specified actor."""
        if self.actor_name == actor_name_to_check and self._is_connected:
            logger.info(f"Disconnecting as actor '{actor_name_to_check}'.")
            await self.disconnect()
        elif self.actor_name == actor_name_to_check:
            logger.info(f"Client was configured as actor '{actor_name_to_check}' but was not connected.")
            # Ensure state is clean
            self.actor_name = None 
            self._is_authenticated = False

    @property
    def is_connected(self) -> bool:
        return self._is_connected and self._ws is not None and self._ws.open

    @property
    def is_authenticated(self) -> bool:
        return self._is_authenticated

    def register_actor_command_handler(
        self, 
        actor_name: str, 
        command_name: str, 
        handler: Callable[..., Coroutine[Any, Any, Any]], 
        parameter_names: List[str]
    ):
        """Internal method to register a command handler for a specific actor this client might become."""
        if actor_name not in self._actor_command_handlers:
            self._actor_command_handlers[actor_name] = {}
            self._actor_command_signatures[actor_name] = {}
        self._actor_command_handlers[actor_name][command_name] = handler
        self._actor_command_signatures[actor_name][command_name] = parameter_names
        logger.debug(f"Locally stored handler for {actor_name}.{command_name} with params: {parameter_names}")

        # If currently connected AND authenticated as this actor, send REGISTER_COMMAND immediately
        if self.actor_name == actor_name and self.is_authenticated:
            # This needs to be an async task
            asyncio.create_task(self._register_command_on_server(actor_name, command_name, parameter_names))

    async def _register_command_on_server(self, actor_name: str, command_name: str, parameter_names: List[str]):
        """Sends the REGISTER_COMMAND message to the server."""
        if not self.is_authenticated:
            logger.warning(f"Cannot register command '{command_name}' for actor '{actor_name}': not authenticated.")
            return
        if self.actor_name != actor_name: # Safety check
            logger.warning(f"Attempted to register command for '{actor_name}' but client is '{self.actor_name}'. Aborting.")
            return

        payload = {
            "type": "REGISTER_COMMAND",
            "actorName": actor_name, # Should be self.actor_name
            "commandName": command_name,
            "parameterTypes": parameter_names, # Sending names for now, server might adapt
        }
        logger.info(f"Sending REGISTER_COMMAND for {actor_name}.{command_name}")
        await self._send_message(payload)
        # No direct response expected for REGISTER_COMMAND in TS, assume fire-and-forget or implicitly handled.

    async def _register_pending_commands(self, actor_name_to_register: str):
        """Registers all locally stored commands for an actor once authenticated as that actor."""
        if actor_name_to_register in self._actor_command_signatures:
            logger.info(f"Registering stored commands for actor '{actor_name_to_register}' with server.")
            for command_name, param_names in self._actor_command_signatures[actor_name_to_register].items():
                await self._register_command_on_server(actor_name_to_register, command_name, param_names)
        else:
            logger.debug(f"No pre-registered command signatures found for actor '{actor_name_to_register}'.")

    async def _send_metadata_update(self, actor_name_to_update: str, metadata: Dict[str, Any]):
        if not self.is_authenticated or self.actor_name != actor_name_to_update:
            logger.warning(f"Cannot update metadata for '{actor_name_to_update}': not connected as this actor or not authenticated.")
            return
        payload = {
            "type": "UPDATE_METADATA", # Assuming this message type exists on server
            "actorName": actor_name_to_update,
            "metadata": metadata
        }
        logger.info(f"Sending UPDATE_METADATA for actor '{actor_name_to_update}'.")
        await self._send_message(payload)
        # Assume no direct response, or handle if server sends one.

    async def actor(self, name: str, metadata: Optional[Dict[str, Any]] = None) -> Actor:
        """
        Get or create an Actor instance. 
        If this Vitrus client instance intends to *be* this actor, call `connect(actor_name=name, metadata=metadata)` first or after.
        The metadata provided here is cached and used if `connect` is called for this actor name.
        """
        logger.info(f"Accessing actor proxy for '{name}'. Metadata provided: {bool(metadata)}")
        if metadata is not None:
             # Cache metadata. This will be used if/when connect() is called for this actor_name.
             self._actor_metadata_cache[name] = metadata 
        
        # Return an Actor proxy instance.
        # The actual connection and command registration happens via Vitrus.connect()
        # and the Actor.on() methods triggering registrations.
        return Actor(self, name, metadata if metadata is not None else self._actor_metadata_cache.get(name, {}))

    # Scene method - placeholder, as in TS
    def scene(self, scene_id: str) -> Any: # Replace Any with Scene class if defined
        logger.warning("Scene functionality is not fully implemented yet.")
        # return Scene(self, scene_id)
        return None

    async def run_command(self, target_actor_name: str, command_name: str, args: Union[Dict[str, Any], List[Any]]) -> Any:
        """Sends a command to a target actor and waits for a response."""
        if not self.is_authenticated:
            # Auto-connect if not authenticated, as an agent (no specific actor_name for the connection itself)
            logger.info("Not authenticated. Auto-connecting as agent to run command...")
            await self.connect() 
            if not self.is_authenticated: # Check again after connect attempt
                raise ConnectionError("Not authenticated. Call connect() first or ensure connection is stable.")

        request_id = self._generate_request_id()
        payload: Dict[str, Any] = {
            "type": "COMMAND",
            "targetActorName": target_actor_name,
            "commandName": command_name,
            "args": args, # Server expects an object/dict for args typically
            "requestId": request_id,
            "sourceChannel": self._redis_channel # If available, for direct reply routing on server
        }
        
        future = asyncio.get_event_loop().create_future()
        self._pending_requests[request_id] = future
        
        await self._send_message(payload)
        logger.info(f"Command '{command_name}' sent to actor '{target_actor_name}' (Request ID: {request_id}). Waiting for response...")
        
        try:
            return await asyncio.wait_for(future, timeout=30.0) # Configurable timeout
        except asyncio.TimeoutError:
            logger.error(f"Timeout waiting for response to command '{command_name}' on actor '{target_actor_name}' (Request ID: {request_id})")
            self._pending_requests.pop(request_id, None) # Clean up
            raise TimeoutError(f"Timeout waiting for response to command '{command_name}'.")

    async def workflow(self, workflow_name: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Executes a named workflow with given parameters."""
        if not self.is_authenticated:
            logger.info("Not authenticated. Auto-connecting as agent to run workflow...")
            await self.connect()
            if not self.is_authenticated:
                raise ConnectionError("Not authenticated. Call connect() first or ensure connection is stable.")

        request_id = self._generate_request_id()
        payload = {
            "type": "WORKFLOW",
            "workflowName": workflow_name,
            "args": params if params is not None else {},
            "requestId": request_id
        }
        
        future = asyncio.get_event_loop().create_future()
        self._pending_requests[request_id] = future
        
        await self._send_message(payload)
        logger.info(f"Workflow '{workflow_name}' requested (Request ID: {request_id}). Waiting for result...")

        try:
            return await asyncio.wait_for(future, timeout=120.0) # Workflows can take longer
        except asyncio.TimeoutError:
            logger.error(f"Timeout waiting for result from workflow '{workflow_name}' (Request ID: {request_id})")
            self._pending_requests.pop(request_id, None)
            raise TimeoutError(f"Timeout waiting for workflow '{workflow_name}' result.")

    async def list_workflows(self) -> List[Dict[str, Any]]:
        """Lists available workflows."""
        if not self.is_authenticated:
            logger.info("Not authenticated. Auto-connecting as agent to list workflows...")
            await self.connect()
            if not self.is_authenticated:
                raise ConnectionError("Not authenticated. Call connect() first or ensure connection is stable.")

        request_id = self._generate_request_id()
        payload = {
            "type": "LIST_WORKFLOWS",
            "requestId": request_id
        }
        
        future = asyncio.get_event_loop().create_future()
        self._pending_requests[request_id] = future
        
        await self._send_message(payload)
        logger.info(f"Requesting list of workflows (Request ID: {request_id})...")

        try:
            # The result from the future should be the list of workflow definitions
            workflows_list = await asyncio.wait_for(future, timeout=30.0) 
            return workflows_list if isinstance(workflows_list, list) else []
        except asyncio.TimeoutError:
            logger.error(f"Timeout waiting for workflow list (Request ID: {request_id})")
            self._pending_requests.pop(request_id, None)
            raise TimeoutError("Timeout waiting for workflow list.")

    # Placeholder for direct image upload if needed, mirroring TS SDK capability
    async def upload_image(self, image_data: bytes, filename: str = "image.png") -> str:
        """ 
        Placeholder for image uploading. 
        The TS SDK had this, but its implementation details (e.g., separate HTTP endpoint or WS message type)
        are not clear from the index.ts snippet alone. Assuming it might be a separate REST call.
        For now, this is a non-functional placeholder.
        """
        logger.warning("upload_image is not implemented yet.")
        raise NotImplementedError("Image uploading is not yet implemented in the Python SDK.")

    # Placeholder for add_record, mirroring TS SDK
    async def add_record(self, data: Any, name: Optional[str] = None) -> str:
        """ 
        Placeholder for adding a record. 
        Similar to upload_image, implementation details require more info.
        """
        logger.warning("add_record is not implemented yet.")
        raise NotImplementedError("Record adding is not yet implemented in the Python SDK.")

    async def __aenter__(self):
        # Allow using `async with Vitrus(...) as v:`
        # The connection logic is more explicit with `await vitrus.connect()`
        # but this ensures a basic level of context management if needed.
        # User still needs to call `await self.connect()` for it to be useful.
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.disconnect()

# Example Usage (for testing, can be removed or moved to an examples folder)
async def example_main():
    api_key = os.environ.get("VITRUS_API_KEY")
    world_id = os.environ.get("VITRUS_WORLD_ID", "test-py-world") # Example world ID

    if not api_key:
        print("Error: VITRUS_API_KEY environment variable not set.")
        return

    print(f"Using API Key: {api_key[:5]}... World ID: {world_id}")

    # ----- Agent/Workflow Example -----
    agent_client = Vitrus(api_key=api_key, world_id=world_id, debug=True)
    try:
        print("\n--- Agent & Workflow Test ---")
        await agent_client.connect() # Connect as an agent
        print("Agent connected.")

        print("Listing workflows...")
        workflows = await agent_client.list_workflows()
        if workflows:
            print(f"Available workflows: {json.dumps(workflows, indent=2)}")
            # Try running the first one if it exists
            first_workflow = workflows[0].get("function", {}).get("name")
            if first_workflow:
                print(f"Running workflow: '{first_workflow}'...")
                try:
                    wf_result = await agent_client.workflow(first_workflow, params={"prompt": "Hello from Python SDK example"})
                    print(f"Workflow '{first_workflow}' result: {json.dumps(wf_result, indent=2)}")
                except Exception as e:
                    print(f"Error running workflow '{first_workflow}': {e}")
            else:
                print("First workflow definition is malformed or has no name.")
        else:
            print("No workflows found.")

    except ConnectionError as e:
        print(f"Agent connection error: {e}")
    except Exception as e:
        print(f"An error occurred with the agent client: {e}")
    finally:
        print("Disconnecting agent client...")
        await agent_client.disconnect()
        print("Agent client disconnected.")

    # ----- Actor Example (Conceptual - usually runs persistently) -----
    # This part would typically run in a separate script/process
    
    # To test actor functionality:
    # 1. Run the actor script (below, adapted) in one terminal.
    # 2. Then run an agent script in another terminal that calls `actor.run()`.

    # Conceptual Actor Setup (example of how one might write it):
    # async def run_actor_process():
    #     actor_client = Vitrus(api_key=api_key, world_id=world_id, debug=True)
    #     try:
    #         print("\n--- Actor Setup (Conceptual) ---")
    #         # Connect AS the actor
    #         await actor_client.connect(actor_name="my_python_actor", metadata={"type": "python_bot", "version": SDK_VERSION})
    #         print(f"Actor 'my_python_actor' connected and authenticated.")

    #         python_actor = await actor_client.actor("my_python_actor") # Get the actor instance, handlers attached to actor_client

    #         async def handle_greet(payload: dict):
    #             name = payload.get("name", "stranger")
    #             print(f"Actor 'my_python_actor' received greet: {payload}")
    #             return f"Hello, {name}! Python actor '{python_actor.name}' is pleased to meet you."
            
    #         python_actor.on("greet", handle_greet) # This registers with actor_client
    #         print("Actor 'my_python_actor' listening for 'greet' commands. Keep this running.")
            
    #         # Keep the actor alive listening
    #         while actor_client.is_connected:
    #             await asyncio.sleep(1)
    #         print("Actor 'my_python_actor' connection lost or disconnected.")

    #     except ConnectionError as e:
    #         print(f"Actor connection error: {e}")
    #     except Exception as e:
    #         print(f"An error occurred with the actor client: {e}")
    #     finally:
    #         print("Disconnecting actor client...")
    #         await actor_client.disconnect()
    #         print("Actor client disconnected.")

    # If you want to run the conceptual actor, uncomment:
    # await run_actor_process()

if __name__ == '__main__':
    # Setup basic logging for the example
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
    # To run this example, ensure VITRUS_API_KEY and optionally VITRUS_WORLD_ID are set.
    # e.g. export VITRUS_API_KEY="your_key_here"
    #      export VITRUS_WORLD_ID="your_world_here"
    try:
        asyncio.run(example_main())
    except KeyboardInterrupt:
        print("\nExample run interrupted by user.") 