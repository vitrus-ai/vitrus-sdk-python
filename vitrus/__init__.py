import asyncio
import atexit
import json
import logging
import uuid
import inspect
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Callable,
    Coroutine,
    Tuple,
    TypeVar,
    Union,
    Mapping,
    Awaitable
)
from enum import Enum

# Using websockets library for Python
import websockets
from websockets.client import WebSocketClientProtocol
from websockets.exceptions import ConnectionClosed, ConnectionClosedError, ConnectionClosedOK

# Setup basic logging
logger = logging.getLogger(__name__)

# --- SDK Version ---
SDK_VERSION = "0.1.0"  # Placeholder, similar to TS fallback
DEFAULT_BASE_URL = "wss://vitrus-dao.onrender.com"

# Best-effort global cleanup for SDK instances (prevents lingering actor/agent sockets).
# NOTE: do not reference `Vitrus` in runtime-evaluated annotations here (module-level annotations
# are evaluated at import time on Python 3.9 unless `from __future__ import annotations`).
_SDK_INSTANCES = set()
_ATEXIT_INSTALLED = False

def _install_atexit_once():
    global _ATEXIT_INSTALLED
    if _ATEXIT_INSTALLED:
        return
    _ATEXIT_INSTALLED = True

    def _cleanup():
        for inst in list(_SDK_INSTANCES):
            try:
                # Try to close gracefully. If no loop, run a temporary loop.
                if hasattr(inst, "_ws") and getattr(inst, "_ws") is not None:
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            asyncio.create_task(inst.close())
                        else:
                            loop.run_until_complete(inst.close())
                    except RuntimeError:
                        asyncio.run(inst.close())
            except Exception:
                pass

    atexit.register(_cleanup)

# --- Message Type Definitions (using TypedDict for clarity) ---
# In Python, we'd typically use dataclasses or TypedDict for these.
# For simplicity matching the TS interfaces, TypedDict is closer.
try:
    from typing import TypedDict, Literal
except ImportError:
    # For Python < 3.8
    from typing_extensions import TypedDict, Literal


class HandshakeMessage(TypedDict):
    type: Literal['HANDSHAKE']
    apiKey: str
    worldId: Optional[str]
    actorName: Optional[str]
    metadata: Optional[Any]


class HandshakeResponseMessage(TypedDict):
    type: Literal['HANDSHAKE_RESPONSE']
    success: bool
    clientId: str
    userId: Optional[str]
    error_code: Optional[str]
    redisChannel: Optional[str]
    message: Optional[str]
    actorInfo: Optional[Dict[str, Any]] # Contains metadata and registeredCommands


class CommandMessage(TypedDict):
    type: Literal['COMMAND']
    targetActorName: str
    commandName: str
    args: List[Any]
    requestId: str
    sourceChannel: Optional[str]


class ResponseMessage(TypedDict):
    type: Literal['RESPONSE']
    targetChannel: str
    requestId: str
    result: Optional[Any]
    error: Optional[str]

class ActorBroadcastEmitMessage(TypedDict, total=False):
    type: Literal['BROADCAST', 'EVENT']  # EVENT kept as legacy alias
    broadcastName: str
    eventName: str  # legacy alias
    args: Any
    worldId: Optional[str]


class ActorBroadcastMessage(TypedDict):
    type: Literal['ACTOR_BROADCAST', 'ACTOR_EVENT']  # ACTOR_EVENT kept as legacy alias
    actorName: str
    actorId: Optional[str]
    worldId: str
    broadcastName: Optional[str]
    eventName: Optional[str]  # legacy alias
    args: Any
    timestamp: str


class RegisterCommandMessage(TypedDict):
    type: Literal['REGISTER_COMMAND']
    actorName: str
    commandName: str
    parameterTypes: List[str]


class WorkflowMessage(TypedDict):
    type: Literal['WORKFLOW']
    workflowName: str
    args: Any
    requestId: str


class WorkflowResultMessage(TypedDict):
    type: Literal['WORKFLOW_RESULT']
    requestId: str
    result: Optional[Any]
    error: Optional[str]


class JSONSchema(TypedDict, total=False):
    type: Literal['object', 'string', 'number', 'boolean', 'array']
    description: Optional[str]
    properties: Optional[Dict[str, 'JSONSchema']]
    required: Optional[List[str]]
    items: Optional['JSONSchema']
    additionalProperties: Optional[bool]


class OpenAITool(TypedDict):
    name: str
    description: Optional[str]
    parameters: JSONSchema
    strict: Optional[bool]


class WorkflowDefinition(TypedDict):
    type: Literal['function']
    function: OpenAITool


class ListWorkflowsMessage(TypedDict):
    type: Literal['LIST_WORKFLOWS']
    requestId: str


class WorkflowListMessage(TypedDict):
    type: Literal['WORKFLOW_LIST']
    requestId: str
    workflows: Optional[List[WorkflowDefinition]]
    error: Optional[str]


# --- Utility for extracting parameter types ---
def get_parameter_types(func: Callable) -> List[str]:
    """
    Extracts parameter type annotations from a function.
    Defaults to 'any' if no annotation is found.
    """
    try:
        sig = inspect.signature(func)
        param_types: List[str] = []
        for param in sig.parameters.values():
            if param.annotation is not inspect.Parameter.empty:
                if hasattr(param.annotation, '__name__'):
                    param_types.append(param.annotation.__name__)
                elif hasattr(param.annotation, '_name') and param.annotation._name: # for things like typing.List
                    param_types.append(str(param.annotation))
                elif hasattr(param.annotation, '__origin__'): # For List[str] etc.
                     param_types.append(str(param.annotation).replace('typing.',''))
                else:
                    param_types.append(str(param.annotation)) 
            else:
                param_types.append('any')
        return param_types
    except (ValueError, TypeError): # inspect.signature can fail on some callables
        try: # Fallback for builtins or weird callables without full signature support
            return ['any'] * len(inspect.getfullargspec(func).args)
        except: # Absolute fallback
             return []


T = TypeVar('T')

# Forward declaration for Vitrus class
class Vitrus:
    pass

# --- Actor Class ---
class Actor:
    _vitrus: "Vitrus"
    _name: str
    _metadata: Dict[str, Any]
    # Command handlers are stored in Vitrus instance: _vitrus._actor_command_handlers

    def __init__(self, vitrus_client: "Vitrus", name: str, metadata: Optional[Dict[str, Any]] = None):
        self._vitrus = vitrus_client
        self._name = name
        self._metadata = metadata if metadata is not None else {}
        # _command_handlers are stored in the Vitrus client instance to keep Actor light

    def on(self, command_name: str, handler: Callable[..., Awaitable[Any]]):
        """
        Register a command handler for this actor.
        The handler can be a regular function or an async function.
        It will be called with arguments passed from the `run` command.
        """
        parameter_types = get_parameter_types(handler)
        self._vitrus.register_actor_command_handler(self._name, command_name, handler, parameter_types)

        if self._vitrus.is_authenticated and self._vitrus.actor_name == self._name:
            asyncio.create_task(self._vitrus.register_command(self._name, command_name, parameter_types))
        elif self._vitrus.debug:
            logger.info(f"[Vitrus SDK - Actor.on] Queuing REGISTER_COMMAND for {command_name} on {self._name}. Will send after auth.")
        return self

    async def run(self, command_name: str, *args: Any) -> Any:
        """
        Run a command on this actor (implies this instance is an agent-side handle).
        """
        return await self._vitrus.run_command(self._name, command_name, list(args))

    def listen(self, event_name: str, handler: Callable[[Any], Any]) -> "Actor":
        """
        Listen for ad-hoc broadcasts emitted by this actor (agent-side handle).
        """
        self._vitrus.register_actor_broadcast_handler(self._name, event_name, handler)
        if not self._vitrus.is_authenticated:
            try:
                asyncio.create_task(self._vitrus.authenticate())
            except RuntimeError:
                # No running loop; user will need to call authenticate() manually.
                pass
        return self

    def broadcast(self, broadcast_name: str, args: Any) -> None:
        """
        Emit an ad-hoc broadcast from this actor to agents in the same world.
        Intended to be called while authenticated as this actor.
        """
        try:
            asyncio.create_task(self._vitrus.send_broadcast(self._name, broadcast_name, args))
        except RuntimeError:
            # No running loop; user should call `await vitrus.send_broadcast(...)`.
            pass

    # Legacy alias
    def event(self, event_name: str, args: Any) -> None:
        self.broadcast(event_name, args)

    @property
    def name(self) -> str:
        return self._name

    @property
    def metadata(self) -> Dict[str, Any]:
        return self._metadata

    def update_metadata(self, new_metadata: Dict[str, Any]):
        self._metadata.update(new_metadata)
        if self._vitrus.actor_name == self._name and self._vitrus.is_authenticated:
            # The TS SDK has a TODO for sending metadata update to server. Mirroring that.
            logger.debug(f"Metadata updated for actor {self._name}. Server update not yet implemented in SDK protocol.")


    def disconnect(self) -> None:
        """
        Disconnect the actor if the SDK is currently connected as this actor.
        """
        self._vitrus.disconnect_if_actor(self._name)


# --- Scene Class (Placeholder as in TS) ---
class Scene:
    _vitrus: "Vitrus"
    _scene_id: str

    def __init__(self, vitrus_client: "Vitrus", scene_id: str):
        self._vitrus = vitrus_client
        self._scene_id = scene_id

    def set_structure(self, structure: Any):
        logger.warning("Scene.set_structure is not yet implemented.")

    def add(self, obj: Any):
        logger.warning("Scene.add is not yet implemented.")

    def update(self, params: Dict[str, Any]):
        logger.warning("Scene.update is not yet implemented.")

    def remove(self, object_id: str):
        logger.warning("Scene.remove is not yet implemented.")

    def get(self) -> Dict[str, Any]:
        logger.warning("Scene.get is not yet implemented.")
        return {"id": self._scene_id}


# --- Main Vitrus Class ---
class Vitrus:
    _ws: Optional[WebSocketClientProtocol]
    _api_key: str
    _world_id: Optional[str]
    _client_id: str
    _is_connected: bool
    _is_authenticated: bool
    _message_handlers: Dict[str, List[Callable[[Dict[str, Any]], None]]]
    _pending_requests: Dict[str, asyncio.Future]
    _actor_command_handlers: Dict[str, Dict[str, Callable[..., Awaitable[Any]]]]
    _actor_command_signatures: Dict[str, Dict[str, List[str]]]
    _actor_metadata: Dict[str, Any]
    _actor_event_handlers: Dict[str, Dict[str, List[Callable[[Any], Any]]]]
    _base_url: str
    _debug: bool
    _current_actor_name: Optional[str]
    _connection_lock: asyncio.Lock
    _receive_task: Optional[asyncio.Task]
    _redis_channel: Optional[str]


    def __init__(self, api_key: str, world: Optional[str] = None, base_url: str = DEFAULT_BASE_URL, debug: bool = False):
        self._api_key = api_key
        self._world_id = world
        self._base_url = base_url
        self._debug = debug

        self._ws = None
        self._client_id = ""
        self._is_connected = False
        self._is_authenticated = False
        self._message_handlers = {}
        self._pending_requests = {}
        self._actor_command_handlers = {}
        self._actor_command_signatures = {}
        self._actor_metadata = {}
        self._actor_event_handlers = {}
        self._current_actor_name = None
        self._connection_lock = asyncio.Lock()
        self._receive_task = None
        self._redis_channel = None

        if self._debug:
            # Ensure logger is configured to show INFO level for debug mode
            if not logger.handlers: # Configure only if no handlers are set
                logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            else: # If handlers exist, just set level for this logger
                logger.setLevel(logging.INFO)
            logger.info(f"[Vitrus v{SDK_VERSION}] Initializing with options: api_key=****, world={world}, base_url={base_url}, debug={debug}")
        else:
            if not logger.handlers:
                logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            else:
                 logger.setLevel(logging.WARNING)

        global _SDK_INSTANCES
        _SDK_INSTANCES.add(self)
        _install_atexit_once()


    async def _connect(self, actor_name_intent: Optional[str] = None, actor_metadata_intent: Optional[Dict[str, Any]] = None) -> None:
        async with self._connection_lock:
            if self._is_connected and self._is_authenticated:
                if actor_name_intent and self._current_actor_name != actor_name_intent:
                    if self._debug: logger.info(f"Re-authenticating as actor: {actor_name_intent}")
                    await self._close_ws_internal()
                elif self._current_actor_name == actor_name_intent:
                    return

            if self._ws:
                 await self._close_ws_internal()

            self._current_actor_name = actor_name_intent or self._current_actor_name
            if self._current_actor_name and actor_metadata_intent:
                self._actor_metadata[self._current_actor_name] = actor_metadata_intent

            query_params = f"?apiKey={self._api_key}"
            if self._world_id:
                query_params += f"&worldId={self._world_id}"
            
            full_url = f"{self._base_url}{query_params}"

            if self._debug:
                logger.info(f"[Vitrus] Attempting to connect to WebSocket server: {full_url}")

            try:
                self._ws = await websockets.connect(full_url, ping_interval=20, ping_timeout=20)
                self._is_connected = True
                if self._debug:
                    logger.info("[Vitrus] WebSocket connection established (pre-handshake).")

                self._receive_task = asyncio.create_task(self._receive_loop())

                handshake_msg: HandshakeMessage = {
                    "type": "HANDSHAKE",
                    "apiKey": self._api_key,
                    "worldId": self._world_id,
                    "actorName": self._current_actor_name,
                    "metadata": self._actor_metadata.get(self._current_actor_name) if self._current_actor_name else None,
                }
                await self._send_message_internal_ws(handshake_msg)

                auth_future = asyncio.Future()
                self._pending_requests["HANDSHAKE_RESPONSE"] = auth_future
                
                try:
                    await asyncio.wait_for(auth_future, timeout=15.0) 
                    self._is_authenticated = auth_future.result() 
                    if not self._is_authenticated:
                         raise ConnectionError(f"Authentication failed: {auth_future.exception() or 'Server rejected handshake'}")
                    if self._debug:
                        logger.info(f"[Vitrus] Authentication successful. Client ID: {self._client_id}, Actor: {self._current_actor_name or 'Agent'}")

                except asyncio.TimeoutError:
                    logger.error("[Vitrus] Authentication timed out.")
                    await self._close_ws_internal(graceful=False)
                    raise ConnectionError("Authentication timed out.")
                except Exception as e:
                    logger.error(f"[Vitrus] Authentication failed during handshake wait: {e}")
                    await self._close_ws_internal(graceful=False)
                    raise ConnectionError(f"Authentication failed: {e}")

            except websockets.exceptions.InvalidURI:
                logger.error(f"[Vitrus] Invalid WebSocket URI: {full_url}")
                self._is_connected = False; self._is_authenticated = False
                raise ConnectionError(f"Invalid WebSocket URI: {full_url}")
            except websockets.exceptions.WebSocketException as e: # Covers handshake errors, connection refused etc.
                logger.error(f"[Vitrus] WebSocket connection failed: {type(e).__name__} - {e}")
                self._is_connected = False; self._is_authenticated = False
                specific_error_msg = str(e)
                if self._world_id:
                    specific_error_msg = f"Connection Failed: Unable to connect to world '{self._world_id}'. This world may not exist, or the API key may be invalid. Original error: {type(e).__name__} - {e}"
                else:
                    specific_error_msg = f"Connection Failed: Unable to establish initial WebSocket connection. Original error: {type(e).__name__} - {e}"
                raise ConnectionError(specific_error_msg)
            except ConnectionError: # Re-raise auth errors from handshake wait
                await self._close_ws_internal(graceful=False)
                raise
            except Exception as e:
                logger.error(f"[Vitrus] An unexpected error occurred during connection: {type(e).__name__} - {e}")
                await self._close_ws_internal(graceful=False)
                self._is_connected = False; self._is_authenticated = False
                raise ConnectionError(f"Unexpected error during connection: {e}")


    async def _close_ws_internal(self, graceful: bool = True):
        """Internal method to close WebSocket and cleanup tasks."""
        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                if self._debug: logger.info("[Vitrus] Message receive task cancelled.")
            except Exception as e:
                if self._debug: logger.info(f"[Vitrus] Exception in receive task during its cancellation: {type(e).__name__} - {e}")
        self._receive_task = None

        if self._ws_is_open():
            try:
                if graceful:
                    await self._ws.close()
                else:
                    # For non-graceful close, try different approaches based on websockets version
                    if hasattr(self._ws, 'close_connection'):
                        # Legacy websockets versions - check if it's a coroutine
                        if inspect.iscoroutinefunction(self._ws.close_connection):
                            await self._ws.close_connection()
                        else:
                            self._ws.close_connection()
                    else:
                        # Newer versions - just close normally but don't wait
                        await self._ws.close()
                if self._debug: logger.info("[Vitrus] WebSocket connection closed.")
            except Exception as e:
                if self._debug: logger.warning(f"[Vitrus] Error while closing WebSocket: {type(e).__name__} - {e}")
        
        self._ws = None
        self._is_connected = False
        self._is_authenticated = False 

        disconnect_error = ConnectionError("Connection Lost: The connection to the Vitrus server was closed.")
        for request_id, fut in list(self._pending_requests.items()): # Iterate over a copy
            if not fut.done():
                fut.set_exception(disconnect_error)
            self._pending_requests.pop(request_id, None)


    async def _receive_loop(self):
        if not self._ws: return
        try:
            async for message_str in self._ws:
                if self._debug and isinstance(message_str, str):
                     try:
                        temp_msg = json.loads(message_str)
                        # Avoid logging entire handshake response in debug unless extremely verbose debugging is on
                        if temp_msg.get("type") != "HANDSHAKE_RESPONSE" or logger.getEffectiveLevel() <= logging.DEBUG - 5:
                            logger.info(f"[Vitrus] Raw message received: {message_str}")
                        elif temp_msg.get("type") == "HANDSHAKE_RESPONSE":
                            logger.info(f"[Vitrus] Received HANDSHAKE_RESPONSE (content partially omitted for brevity in standard debug).")
                     except json.JSONDecodeError:
                         logger.info(f"[Vitrus] Raw non-JSON message received: {message_str}")

                try:
                    message = json.loads(message_str)
                    self._handle_message(message)
                except json.JSONDecodeError:
                    logger.error(f"Error parsing WebSocket message as JSON: {message_str[:200]}...") # Log snippet
                except Exception as e: # Catch broad errors in message handling
                    logger.error(f"Error processing message: {type(e).__name__} - {e}. Message snippet: {message_str[:200]}...")
        
        except ConnectionClosedOK:
            if self._debug: logger.info("[Vitrus] WebSocket connection closed normally by server.")
        except ConnectionClosedError as e: # Connection closed with an error code
            logger.error(f"[Vitrus] WebSocket connection closed with error: Code {e.code}, Reason: '{e.reason}'")
        except ConnectionClosed as e: # More general closed connection
            logger.error(f"[Vitrus] WebSocket connection closed unexpectedly: Code {e.code}, Reason: '{e.reason}'")
        except asyncio.CancelledError:
            if self._debug: logger.info("[Vitrus] Receive loop was cancelled.")
            # Do not re-raise, cancellation is part of shutdown
        except Exception as e: # Other unexpected errors in the loop itself
            if self._debug or not isinstance(e, websockets.exceptions.WebSocketException): # Don't be too noisy for common WS closure exceptions if not debugging
                logger.error(f"[Vitrus] Unexpected error in receive loop: {type(e).__name__} - {e}")
        finally:
            if self._debug: logger.info("[Vitrus] Exiting receive loop.")
            # Ensure connection state is updated if loop exits unexpectedly
            if self._is_connected or self._is_authenticated: # If it wasn't an intentional close from our side that already cleaned up
                 await self._close_ws_internal(graceful=False)


    async def _send_message_internal_ws(self, message: Dict[str, Any]):
        if self._ws_is_open():
            msg_str = json.dumps(message)
            if self._debug:
                log_msg = msg_str
                if message.get("type") == "HANDSHAKE": log_msg = "(Handshake message with API key redacted)"
                logger.info(f"[Vitrus] Sending message: {log_msg}")
            await self._ws.send(msg_str)
        else:
            logger.error("[Vitrus] WebSocket not connected or not open. Cannot send message.")
            raise ConnectionError("WebSocket is not connected or not open.")

    async def _send_message_public(self, message: Dict[str, Any]):
        # If not connected, attempt to authenticate.
        # self.authenticate() should robustly attempt connection and authentication.
        # It returns True on success, False on failure, or raises an exception for critical setup issues.
        if not self._is_connected or not self._ws_is_open():
            if self._debug: 
                logger.info("[Vitrus] _send_message_public: Not connected. Attempting to authenticate.")
            
            auth_successful = await self.authenticate() # authenticate() will try to connect.
            
            if not auth_successful:
                # If authenticate() explicitly returns False, it means it tried and determined a failure
                # (e.g., handshake failed but connection didn't immediately drop, or API key invalid).
                # The state (_is_connected, _is_authenticated) should reflect this.
                logger.error("[Vitrus] _send_message_public: Authentication attempt returned False. Cannot send message.")
                raise ConnectionError("Failed to authenticate or establish a viable connection.")
            
            # If auth_successful is True, then self.authenticate() believes it has succeeded.
            # self._is_connected and self._is_authenticated should be True.
            # We still need to check the actual WebSocket state immediately before sending,
            # as a rapid disconnection could have occurred.

        # Final check: even if authentication was successful (or thought to be),
        # is the WebSocket genuinely open RIGHT NOW?
        if not self._is_connected or not self._ws_is_open():
            # This path means:
            # 1. We were initially connected, but it dropped before this check.
            # 2. We attempted re-authentication, it *claimed* success (returned True),
            #    but the connection is already gone (e.g., _receive_loop died and reset flags).
            # This is the scenario matching the user's error, pointing to an unstable connection
            # or a race where the connection drops right after the handshake.
            logger.warning("[Vitrus] _send_message_public: Connection found to be closed immediately before sending, "
                           "despite prior checks or authentication attempts. This may indicate an unstable connection or immediate post-handshake closure.")
            raise ConnectionError(
                "Connection is not active. It may have been lost immediately after being established or during an authentication attempt."
            )

        # If all checks pass, attempt to send.
        try:
            await self._send_message_internal_ws(message)
        except ConnectionError as e: # Catch specific ConnectionError from _send_message_internal_ws
            logger.error(f"[Vitrus] _send_message_public: Connection error during the actual send operation: {e}")
            # It's possible the connection dropped at the exact moment of sending.
            # Ensure state is fully reset if this happens.
            if self._is_connected or self._is_authenticated: # Avoid redundant call if already closing
                await self._close_ws_internal(graceful=False) # Reset state
            raise # Re-raise the ConnectionError from _send_message_internal_ws
        except Exception as e:
            logger.error(f"[Vitrus] _send_message_public: Unexpected error during send: {type(e).__name__} - {e}")
            if self._is_connected or self._is_authenticated: # Avoid redundant call if already closing
                await self._close_ws_internal(graceful=False) # Reset state
            raise # Re-raise as a generic error or wrap it


    def _handle_message(self, message: Dict[str, Any]):
        msg_type = message.get("type")
        
        # More selective logging for HANDSHAKE_RESPONSE
        if msg_type == "HANDSHAKE_RESPONSE":
            if self._debug: logger.info(f"[Vitrus] Handling HANDSHAKE_RESPONSE.")
        elif self._debug:
             logger.info(f"[Vitrus] Handling message of type: {msg_type}, RequestID: {message.get('requestId')}")


        if msg_type == "HANDSHAKE_RESPONSE":
            response = message # No need to cast to TypedDict for internal handling if careful
            auth_future = self._pending_requests.pop("HANDSHAKE_RESPONSE", None)
            if response.get("success"):
                self._client_id = response.get("clientId", "")
                self._redis_channel = response.get("redisChannel")
                if self._debug: logger.info(f"[Vitrus] Handshake successful. Client ID: {self._client_id}.")
                
                actor_info = response.get("actorInfo")
                if actor_info and self._current_actor_name:
                    self._actor_metadata[self._current_actor_name] = actor_info.get("metadata", {})
                    registered_cmds = actor_info.get("registeredCommands", [])
                    if registered_cmds:
                        if self._current_actor_name not in self._actor_command_signatures:
                            self._actor_command_signatures[self._current_actor_name] = {}
                        for cmd_def in registered_cmds: # cmd_def is dict like {"name": "cmd_name", "parameterTypes": []}
                            self._actor_command_signatures[self._current_actor_name][cmd_def["name"]] = cmd_def["parameterTypes"]
                
                if auth_future and not auth_future.done(): auth_future.set_result(True)
            else:
                err_msg = response.get("message", "Handshake failed due to unknown server error.")
                err_code = response.get("error_code", "UNKNOWN_ERROR")
                logger.error(f"Handshake failed: {err_msg} (Error code: {err_code})")
                if auth_future and not auth_future.done(): auth_future.set_exception(ConnectionError(f"Authentication failed: {err_msg} (Code: {err_code})"))
            return

        if msg_type in ("ACTOR_BROADCAST", "ACTOR_EVENT"):
            self._dispatch_actor_broadcast(message)  # type: ignore[arg-type]
            return

        request_id = message.get("requestId")
        pending_future = self._pending_requests.get(request_id) if request_id else None

        if msg_type == "COMMAND":
            self._handle_command_message(message) # Changed to avoid direct TypedDict casting here
            return

        if msg_type in ["RESPONSE", "WORKFLOW_RESULT", "WORKFLOW_LIST"]:
            if pending_future:
                self._pending_requests.pop(request_id) # Remove once we attempt to resolve
                error = message.get("error")
                if error:
                    if self._debug: logger.info(f"[Vitrus] Received error for {request_id}: {error}")
                    if not pending_future.done(): pending_future.set_exception(RuntimeError(error))
                else:
                    result_data = None
                    if msg_type == "RESPONSE": result_data = message.get("result")
                    elif msg_type == "WORKFLOW_RESULT": result_data = message.get("result")
                    elif msg_type == "WORKFLOW_LIST": result_data = message.get("workflows", [])
                    
                    if self._debug: logger.info(f"[Vitrus] Received result for {request_id}: {str(result_data)[:100]}...")
                    if not pending_future.done(): pending_future.set_result(result_data)
            elif request_id:
                logger.warning(f"Received message for unknown or already handled request ID: {request_id}, Type: {msg_type}")
            return

        handlers = self._message_handlers.get(msg_type, [])
        for handler_fn in handlers:
            try: handler_fn(message)
            except Exception as e: logger.error(f"Error in custom message handler for type {msg_type}: {e}")

    def _dispatch_actor_broadcast(self, message: Dict[str, Any]) -> None:
        actor_name = message.get("actorName")
        event_name = message.get("broadcastName") or message.get("eventName")
        args = message.get("args")

        if not isinstance(actor_name, str) or not isinstance(event_name, str):
            if self._debug: logger.info(f"[Vitrus] Received malformed ACTOR_EVENT: {message}")
            return

        handlers = self._actor_event_handlers.get(actor_name, {}).get(event_name, [])
        for handler in handlers:
            try:
                if inspect.iscoroutinefunction(handler):
                    asyncio.create_task(handler(args))  # type: ignore[misc]
                else:
                    handler(args)
            except Exception as e:
                logger.error(f"Error in actor event handler for {actor_name}.{event_name}: {e}")

    def _handle_command_message(self, command_msg_data: Dict[str,Any]):
        actor_name = command_msg_data.get("targetActorName")
        command_name = command_msg_data.get("commandName")
        args = command_msg_data.get("args", [])
        request_id = command_msg_data.get("requestId")
        source_channel = command_msg_data.get("sourceChannel")

        if not all([actor_name, command_name, request_id]): # source_channel is optional
            logger.error(f"Received malformed COMMAND message: {command_msg_data}")
            return

        if self._debug: logger.info(f"[Vitrus] Handling command: '{command_name}' for actor '{actor_name}' with args: {args}")

        actor_handlers = self._actor_command_handlers.get(actor_name, {})
        handler = actor_handlers.get(command_name)

        async def execute_and_respond_task():
            response_payload: Dict[str, Any] = {"type": "RESPONSE", "targetChannel": source_channel or "", "requestId": request_id}
            if not handler:
                logger.warning(f"No handler for command '{command_name}' on actor '{actor_name}'.")
                response_payload["error"] = f"Command '{command_name}' not found on actor '{actor_name}'."
            else:
                try:
                    if inspect.iscoroutinefunction(handler): result = await handler(*args)
                    else: # Run sync handler in thread pool executor
                        loop = asyncio.get_running_loop()
                        result = await loop.run_in_executor(None, handler, *args)
                    if self._debug: logger.info(f"Command '{command_name}' on '{actor_name}' executed. Result: {str(result)[:100]}...")
                    response_payload["result"] = result
                except Exception as e:
                    logger.error(f"Error executing command '{command_name}' on '{actor_name}': {type(e).__name__} - {e}", exc_info=self._debug)
                    response_payload["error"] = str(e)
            
            try: # Ensure response is sent even if main connection drops during execution
                 await self._send_message_public(response_payload) # type: ignore
            except ConnectionError as ce:
                 logger.error(f"ConnectionError sending response for {request_id}: {ce}")
            except Exception as e:
                 logger.error(f"Unexpected error sending response for {request_id}: {e}")

        asyncio.create_task(execute_and_respond_task())


    def _generate_request_id(self) -> str:
        return uuid.uuid4().hex[:13]

    async def authenticate(self, actor_name: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> bool:
        if self._debug: logger.info(f"[Vitrus] Authenticating..." + (f" (as actor: {actor_name})" if actor_name else " (as agent)"))

        if actor_name and not self._world_id:
            raise ValueError("Vitrus SDK requires a world_id to authenticate as an actor.")

        if self._is_authenticated and actor_name == self._current_actor_name:
            if self._debug: logger.info(f"Already authenticated as {actor_name or 'agent'}.")
            return True
        
        # If switching actor or initial auth, (re)connect
        self._current_actor_name = actor_name # Set intent
        if actor_name and metadata: self._actor_metadata[actor_name] = metadata
        
        try:
            await self._connect(actor_name_intent=actor_name, actor_metadata_intent=metadata)
            if self._is_authenticated and self._current_actor_name: # Successfully authenticated as specific actor
                await self._register_pending_commands(self._current_actor_name)
        except ConnectionError as e: # Covers connection and auth failures from _connect
            logger.error(f"[Vitrus] Authentication failed: {e}")
            # _connect should have reset state, but ensure here too
            self._is_authenticated = False; self._is_connected = False
            self._current_actor_name = None 
            return False
        except Exception as e: # Other unexpected errors
            logger.error(f"[Vitrus] Unexpected error during authentication process: {type(e).__name__} - {e}")
            await self._close_ws_internal(graceful=False) # Ensure WS is down
            return False

        return self._is_authenticated


    def register_actor_command_handler(
        self, actor_name: str, command_name: str,
        handler: Callable[..., Awaitable[Any]], parameter_types: List[str]
    ):
        if self._debug: logger.info(f"[Vitrus] Registering local command handler for actor '{actor_name}', command '{command_name}'")
        
        if actor_name not in self._actor_command_handlers: self._actor_command_handlers[actor_name] = {}
        self._actor_command_handlers[actor_name][command_name] = handler

        if actor_name not in self._actor_command_signatures: self._actor_command_signatures[actor_name] = {}
        self._actor_command_signatures[actor_name][command_name] = parameter_types

    def register_actor_broadcast_handler(self, actor_name: str, broadcast_name: str, handler: Callable[[Any], Any]) -> None:
        if actor_name not in self._actor_event_handlers:
            self._actor_event_handlers[actor_name] = {}
        if broadcast_name not in self._actor_event_handlers[actor_name]:
            self._actor_event_handlers[actor_name][broadcast_name] = []
        self._actor_event_handlers[actor_name][broadcast_name].append(handler)

    # Legacy alias
    def register_actor_event_handler(self, actor_name: str, event_name: str, handler: Callable[[Any], Any]) -> None:
        self.register_actor_broadcast_handler(actor_name, event_name, handler)

    async def send_broadcast(self, actor_name: str, broadcast_name: str, args: Any) -> None:
        if not self._is_authenticated or self._current_actor_name != actor_name:
            if self._debug:
                logger.info(f"[Vitrus] Not broadcasting '{broadcast_name}' - not authenticated as actor '{actor_name}'.")
            return
        msg: ActorBroadcastEmitMessage = {"type": "BROADCAST", "broadcastName": broadcast_name, "eventName": broadcast_name, "args": args}
        await self._send_message_public(msg)  # type: ignore[arg-type]

    # Legacy alias
    async def send_event(self, actor_name: str, event_name: str, args: Any) -> None:
        await self.send_broadcast(actor_name, event_name, args)


    async def register_command(self, actor_name: str, command_name: str, parameter_types: List[str]):
        if not self._is_authenticated or self._current_actor_name != actor_name:
            if self._debug: logger.info(f"[Vitrus] Queuing server registration for command '{command_name}' on actor '{actor_name}'. Will send after full auth as this actor.")
            return # Will be handled by _register_pending_commands

        if self._debug: logger.info(f"[Vitrus] Registering command with server: actor='{actor_name}', command='{command_name}'")
        message: RegisterCommandMessage = {"type": "REGISTER_COMMAND", "actorName": actor_name, "commandName": command_name, "parameterTypes": parameter_types}
        await self._send_message_public(message)


    async def _register_pending_commands(self, actor_name: str):
        if not self._is_authenticated or self._current_actor_name != actor_name: return
        if self._debug: logger.info(f"[Vitrus] Registering any pending commands for actor '{actor_name}' with the server.")
        
        signatures = self._actor_command_signatures.get(actor_name, {})
        handlers = self._actor_command_handlers.get(actor_name,{})

        for cmd_name, param_types in signatures.items():
            if cmd_name in handlers:
                try:
                    if self._debug: logger.info(f"Sending actual REGISTER_COMMAND for {actor_name}.{cmd_name}")
                    # This call to register_command will now pass the auth check
                    await self.register_command(actor_name, cmd_name, param_types)
                except Exception as e: logger.error(f"Error registering pending command {cmd_name} for actor {actor_name}: {e}")


    async def actor(self, name: str, options: Optional[Dict[str, Any]] = None) -> Actor:
        if self._debug: logger.info(f"[Vitrus] actor(name='{name}', options={'provided' if options is not None else 'not provided'})")

        if options is not None and not self._world_id:
            raise ValueError("Vitrus SDK requires a world_id to create/authenticate as an actor with options.")

        current_meta = self._actor_metadata.get(name, {})
        if options is not None: current_meta.update(options); self._actor_metadata[name] = current_meta
        
        actor_instance = Actor(self, name, current_meta)

        if options is not None: # Intent to BE this actor
            # Authenticate if not already this actor, or if metadata changed implying re-auth might be needed
            if not self._is_authenticated or self._current_actor_name != name:
                 if self._debug: logger.info(f"[Vitrus] Options provided for actor '{name}'. Ensuring authentication as this actor...")
                 await self.authenticate(name, self._actor_metadata.get(name)) # This will also call _register_pending_commands on success
            else: # Already authenticated as this actor
                 if self._debug: logger.info(f"Already authenticated as actor '{name}'. Ensuring commands are registered.")
                 await self._register_pending_commands(name) # Ensure commands are up-to-date
        return actor_instance


    def scene(self, scene_id: str) -> Scene:
        if self._debug: logger.info(f"[Vitrus] Getting scene handle for ID: {scene_id}")
        return Scene(self, scene_id)


    async def run_command(self, actor_name: str, command_name: str, args: List[Any]) -> Any:
        if self._debug: logger.info(f"[Vitrus] run_command: actor='{actor_name}', command='{command_name}', args={args}")
        if not self._world_id: raise ValueError("Vitrus SDK requires a world_id to run commands on actors.")
        if not self._is_authenticated: await self.authenticate() # Authenticate as agent

        request_id = self._generate_request_id()
        command_msg: CommandMessage = {"type": "COMMAND", "targetActorName": actor_name, "commandName": command_name, "args": args, "requestId": request_id}

        fut = asyncio.Future()
        self._pending_requests[request_id] = fut
        try:
            await self._send_message_public(command_msg)
            return await asyncio.wait_for(fut, timeout=30.0)
        except asyncio.TimeoutError:
            self._pending_requests.pop(request_id, None)
            logger.error(f"Timeout waiting for response to command '{command_name}' on actor '{actor_name}' (req ID: {request_id})")
            raise TimeoutError(f"Timeout for command '{command_name}' on '{actor_name}'")
        except Exception as e: # Includes ConnectionError from _send_message_public if auth fails
            self._pending_requests.pop(request_id, None)
            raise


    async def workflow(self, workflow_name: str, args: Optional[Dict[str, Any]] = None) -> Any:
        if self._debug: logger.info(f"[Vitrus] Running workflow: '{workflow_name}' with args: {args}")
        # Preserve existing authentication state - don't force re-auth as agent
        if not self._is_authenticated: 
            # If we have a current actor name, re-authenticate as that actor
            if self._current_actor_name:
                await self.authenticate(self._current_actor_name, self._actor_metadata.get(self._current_actor_name))
            else:
                await self.authenticate()  # Authenticate as agent only if no actor context

        request_id = self._generate_request_id()
        workflow_msg: WorkflowMessage = {"type": "WORKFLOW", "workflowName": workflow_name, "args": args if args is not None else {}, "requestId": request_id}

        fut = asyncio.Future()
        self._pending_requests[request_id] = fut
        try:
            await self._send_message_public(workflow_msg)
            return await asyncio.wait_for(fut, timeout=300.0) # 5 min timeout for workflow
        except asyncio.TimeoutError:
            self._pending_requests.pop(request_id, None)
            logger.error(f"Timeout waiting for result of workflow '{workflow_name}' (req ID: {request_id})")
            raise TimeoutError(f"Timeout for workflow '{workflow_name}'")
        except Exception as e:
            self._pending_requests.pop(request_id, None)
            raise

    async def list_workflows(self) -> List[WorkflowDefinition]:
        if self._debug: logger.info("[Vitrus] Requesting list of available workflows.")
        # Preserve existing authentication state - don't force re-auth as agent
        if not self._is_authenticated:
            # If we have a current actor name, re-authenticate as that actor
            if self._current_actor_name:
                await self.authenticate(self._current_actor_name, self._actor_metadata.get(self._current_actor_name))
            else:
                await self.authenticate()  # Authenticate as agent only if no actor context

        request_id = self._generate_request_id()
        list_msg: ListWorkflowsMessage = {"type": "LIST_WORKFLOWS", "requestId": request_id}

        fut = asyncio.Future()
        self._pending_requests[request_id] = fut
        try:
            await self._send_message_public(list_msg)
            return await asyncio.wait_for(fut, timeout=30.0)
        except asyncio.TimeoutError:
            self._pending_requests.pop(request_id, None)
            logger.error(f"Timeout waiting for workflow list (req ID: {request_id})")
            raise TimeoutError("Timeout waiting for workflow list")
        except Exception as e:
            self._pending_requests.pop(request_id, None)
            raise

    async def upload_image(self, image_data: bytes, filename: str = "image.png") -> str:
        if self._debug: logger.info(f"[Vitrus] Mock uploading image: {filename} (size: {len(image_data)} bytes)")
        logger.warning("Vitrus.upload_image is a mock implementation.")
        return f"https://vitrus.io/images/{filename}" # Mock URL

    async def add_record(self, data: Dict[str, Any], name: Optional[str] = None) -> str:
        if self._debug: logger.info(f"[Vitrus] Mock adding record: {name or 'untitled'} with data: {data}")
        logger.warning("Vitrus.add_record is a mock implementation.")
        return name or self._generate_request_id()

    @property
    def is_authenticated(self) -> bool: return self._is_authenticated
    @property
    def actor_name(self) -> Optional[str]: return self._current_actor_name
    @property
    def client_id(self) -> str: return self._client_id
    @property
    def debug(self) -> bool: return self._debug

    def disconnect_if_actor(self, actor_name_to_disconnect: str) -> None:
        if self._current_actor_name == actor_name_to_disconnect and self._is_authenticated:
            if self._ws_is_open():
                if self._debug: logger.info(f"[Vitrus] Actor '{actor_name_to_disconnect}' is requesting SDK disconnect.")
                asyncio.create_task(self._close_ws_internal(graceful=True)) # Schedule close
            else: # Authenticated as actor, but WS not open.
                 if self._debug: logger.info(f"[Vitrus] disconnect_if_actor: WS for '{actor_name_to_disconnect}' not open, but was auth'd. Cleaning state.")
                 self._is_authenticated = False; self._is_connected = False; self._current_actor_name = None
        elif self._debug:
            logger.info(f"[Vitrus] disconnect_if_actor: SDK not connected as '{actor_name_to_disconnect}' (currently: {self._current_actor_name or 'agent/none'}). No action.")

    async def close(self) -> None:
        """Gracefully closes the WebSocket connection and cleans up resources."""
        if self._debug: logger.info("[Vitrus] Initiating graceful shutdown of SDK instance.")
        await self._close_ws_internal(graceful=True)
        _SDK_INSTANCES.discard(self)

    def _ws_is_open(self) -> bool:
        """
        Helper to check if the websocket connection is open.
        Compatible with different websockets library versions.
        """
        if self._ws is None:
            return False
        
        # Try different approaches based on websockets library version
        try:
            # For newer websockets versions (15.x+) - check state
            if hasattr(self._ws, 'state'):
                from websockets.protocol import State
                return self._ws.state == State.OPEN
        except (ImportError, AttributeError):
            pass
        
        try:
            # For legacy versions - check open property
            if hasattr(self._ws, 'open'):
                return self._ws.open
        except AttributeError:
            pass
        
        try:
            # For some versions - check closed property
            if hasattr(self._ws, 'closed'):
                return not self._ws.closed
        except AttributeError:
            pass
        
        # Fallback - assume open if websocket exists and no explicit close method available
        return True
