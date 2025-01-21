import asyncio
import base64
import importlib
import json
import os
import ssl
import time

import websockets
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import Connect, VoiceResponse

from config import REALTIME_AUDIO_API_URL

# Create an SSL context (for development purposes only)
ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE


load_dotenv()

# Configuration
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PORT = int(os.getenv("PORT", 8080))

LOG_EVENT_TYPES = [
    "error",
    "response.content.done",
    "rate_limits.updated",
    "response.done",
    "input_audio_buffer.committed",
    "input_audio_buffer.speech_stopped",
    "input_audio_buffer.speech_started",
    "session.created",
    "response.function_call_arguments.done",
    "conversation.item.created",
    "response.audio_transcript.done",
    "response.output_item.added",
    "response.content_part.done",
    "response.content_part.added",
]
SHOW_TIMING_MATH = False

INSTRUCTIONS = ""
GREETING_TEXT = ""
VOICE = ""
INTRO = ""
ADVANCED_SETTINGS = {}
TOOLS_SCHEMA = []
TOOLS = []


if not OPENAI_API_KEY:
    raise ValueError("Missing the OpenAI API key. Please set it in the .env file.")

app = FastAPI()


@app.get("/", response_class=JSONResponse)
async def index_page():
    load_metadata("maintenance")
    return {"message": INTRO}


@app.api_route("/incoming-call/{type}", methods=["GET", "POST"])
async def handle_incoming_call(request: Request, type: str):
    """Handle incoming call and return TwiML response to connect to Media Stream."""
    response = VoiceResponse()
    load_metadata(type)
    # <Say> punctuation to improve text-to-speech flow
    if INTRO:
        response.say(INTRO)
        response.pause(length=1)
    host = request.url.hostname
    connect = Connect()
    connect.stream(url=f"wss://{host}/media-stream/{type}")
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")


@app.websocket("/media-stream/{type}")
async def handle_media_stream(websocket: WebSocket, type: str):
    """Handle WebSocket connections between Twilio and OpenAI."""
    try:
        print("Client connected")
        TOOL_MAP = {tool.__name__: tool for tool in TOOLS}
        print(f"Current tools: {TOOL_MAP} \n schema: {TOOLS_SCHEMA}")

        await websocket.accept()
        responses = []

        async with websockets.connect(
            REALTIME_AUDIO_API_URL,
            extra_headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "OpenAI-Beta": "realtime=v1",
            },
            ssl=ssl_context,
        ) as openai_ws:
            await initialize_session(openai_ws)

            # Connection specific state
            stream_sid = None
            latest_media_timestamp = 0
            last_assistant_item = None
            mark_queue = []
            response_start_timestamp_twilio = None

            async def receive_from_twilio():
                """Receive audio data from Twilio and send it to the OpenAI Realtime API."""
                nonlocal stream_sid, latest_media_timestamp
                try:
                    async for message in websocket.iter_text():
                        data = json.loads(message)
                        if data["event"] == "media" and openai_ws.open:
                            latest_media_timestamp = int(data["media"]["timestamp"])
                            audio_append = {
                                "type": "input_audio_buffer.append",
                                "audio": data["media"]["payload"],
                            }
                            await openai_ws.send(json.dumps(audio_append))
                        elif data["event"] == "start":
                            stream_sid = data["start"]["streamSid"]
                            print(f"Incoming stream has started {stream_sid}")
                            response_start_timestamp_twilio = None
                            latest_media_timestamp = 0
                            last_assistant_item = None
                        elif data["event"] == "mark":
                            if mark_queue:
                                mark_queue.pop(0)
                except WebSocketDisconnect:
                    print("Client disconnected.")
                    if openai_ws.open:
                        await openai_ws.close()

            async def send_to_twilio():
                """Receive events from the OpenAI Realtime API, send audio back to Twilio."""
                nonlocal stream_sid, last_assistant_item, response_start_timestamp_twilio
                try:
                    async for openai_message in openai_ws:
                        response = json.loads(openai_message)
                        response_type = response.get("type", "")
                        if response_type in LOG_EVENT_TYPES:
                            print(f"Received event: {response_type}", response)

                        if (
                            response_type == "response.audio.delta"
                            and "delta" in response
                        ):
                            audio_payload = base64.b64encode(
                                base64.b64decode(response["delta"])
                            ).decode("utf-8")
                            audio_delta = {
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": audio_payload},
                            }
                            await websocket.send_json(audio_delta)

                            if response_start_timestamp_twilio is None:
                                response_start_timestamp_twilio = latest_media_timestamp
                                if SHOW_TIMING_MATH:
                                    print(
                                        f"Setting start timestamp for new response: {response_start_timestamp_twilio}ms"
                                    )

                            # Update last_assistant_item safely
                            if response.get("item_id"):
                                last_assistant_item = response["item_id"]

                            await send_mark(websocket, stream_sid)

                        # if (
                        #     response_type
                        #     == "response.function_call_arguments.delta"
                        #     and "delta" in response
                        # ):

                        #     # Path to your .mp3 file
                        #     file_path = "./usecases/maintenance/custom-audio.wav"

                        #     # Read the .mp3 file in binary mode
                        #     with open(file_path, "rb") as audio_file:
                        #         # Encode the file content to Base64
                        #         audio_payload = base64.b64encode(
                        #             audio_file.read()
                        #         ).decode("utf-8")

                        #         audio_delta = {
                        #             "event": "media",
                        #             "streamSid": stream_sid,
                        #             "media": {"payload": audio_payload},
                        #         }

                        #         await websocket.send_json(audio_delta)

                        if (
                            response_type == "response.created"
                            or response_type == "response.done"
                        ):
                            current_response = response.get("response", {})
                            response_id = current_response.get("id")
                            status = current_response.get("status")

                            # Check if the response_id already exists
                            existing_response = next(
                                (
                                    r
                                    for r in responses
                                    if r["response_id"] == response_id
                                ),
                                None,
                            )

                            if existing_response:
                                # Update the status if the response_id exists
                                existing_response["status"] = status
                            else:
                                # Otherwise, add a new entry
                                responses.append(
                                    {"response_id": response_id, "status": status}
                                )

                        if response_type == "response.function_call_arguments.done":
                            try:
                                call_id = response.get("call_id")
                                function_name = response.get("name")
                                args = (
                                    json.loads(response.get("arguments", "{}"))
                                    if "arguments" in response
                                    else {}
                                )

                                result = ""

                                # List of different intermediate messages to rotate through
                                intermediate_messages = [
                                    "I'm processing your request, this will just take a moment...",
                                    "Working on getting that information for you...",
                                    "Almost there, retrieving the data you need...",
                                    "Just a few more seconds while I gather the details...",
                                    "Processing your request, thank you for your patience...",
                                ]

                                is_last_response_active = (
                                    responses[-1]["status"] == "in_progress"
                                    if responses
                                    else False
                                )
                                print(
                                    f"responses: {responses}, is_last_response_active: {is_last_response_active}"
                                )

                                await send_conversation_item(
                                    openai_ws,
                                    f"Respond to the user with wait message. {intermediate_messages[0]}",
                                    is_last_response_active,
                                )

                                # Start async timer for updating messages
                                message_index = 1
                                start_time = time.time()

                                tool_to_invoke = TOOL_MAP.get(function_name)
                                if tool_to_invoke:
                                    # If the function call takes longer than 2 seconds, send another intermediate message
                                    while result == "":
                                        if time.time() - start_time > 2:
                                            await send_conversation_item(
                                                openai_ws,
                                                f"Respond to the user with wait message. {intermediate_messages[message_index % len(intermediate_messages)]}",
                                                is_last_response_active,
                                            )
                                            message_index += 1
                                            start_time = time.time()

                                        # Try to get the result
                                        result = tool_to_invoke(**args)

                                        if result:
                                            print(
                                                f"Received function call: {function_name} | {call_id} with args: {args}, {result}"
                                            )
                                            break

                                else:
                                    print(
                                        f"Tool '{function_name}' not found in TOOL_MAP."
                                    )

                                # Send function output
                                function_output_event = {
                                    "type": "conversation.item.create",
                                    "item": {
                                        "type": "function_call_output",
                                        "call_id": call_id,
                                        "output": result,
                                    },
                                }
                                await openai_ws.send(json.dumps(function_output_event))

                                # Send final response
                                response_create_event = {
                                    "type": "response.create",
                                    "response": {
                                        "modalities": ["text", "audio"],
                                        "instructions": f"Formulate an answer strictly based on the provided context chunks without adding external knowledge or assumptions. context: {result}. Be concise and friendly.",
                                    },
                                }
                                await openai_ws.send(json.dumps(response_create_event))

                            except Exception as e:
                                print("Error processing question via Assistant:", e)
                                await send_conversation_item(
                                    openai_ws,
                                    f"Respond to the user with apologetic message. ' apologize, but I'm having trouble processing your request right now. Is there anything else I can help you with?'",
                                )

                        # Trigger an interruption. Your use case might work better using `input_audio_buffer.speech_stopped`, or combining the two.
                        if response_type == "input_audio_buffer.speech_started":
                            print("Speech started detected.")
                            if last_assistant_item:
                                print(
                                    f"Interrupting response with id: {last_assistant_item}"
                                )
                                await handle_speech_started_event()

                        if response_type == "error":
                            print("Error from OpenAI:", response["error"]["message"])

                except Exception as e:
                    print(f"Error in send_to_twilio: {e}")

            async def handle_speech_started_event():
                """Handle interruption when the caller's speech starts."""
                nonlocal response_start_timestamp_twilio, last_assistant_item
                print("Handling speech started event.")
                if mark_queue and response_start_timestamp_twilio is not None:
                    elapsed_time = (
                        latest_media_timestamp - response_start_timestamp_twilio
                    )
                    if SHOW_TIMING_MATH:
                        print(
                            f"Calculating elapsed time for truncation: {latest_media_timestamp} - {response_start_timestamp_twilio} = {elapsed_time}ms"
                        )

                    if last_assistant_item:
                        if SHOW_TIMING_MATH:
                            print(
                                f"Truncating item with ID: {last_assistant_item}, Truncated at: {elapsed_time}ms"
                            )

                        truncate_event = {
                            "type": "conversation.item.truncate",
                            "item_id": last_assistant_item,
                            "content_index": 0,
                            "audio_end_ms": elapsed_time,
                        }
                        await openai_ws.send(json.dumps(truncate_event))

                    await websocket.send_json(
                        {"event": "clear", "streamSid": stream_sid}
                    )

                    mark_queue.clear()
                    last_assistant_item = None
                    response_start_timestamp_twilio = None

            async def send_mark(connection, stream_sid):
                if stream_sid:
                    mark_event = {
                        "event": "mark",
                        "streamSid": stream_sid,
                        "mark": {"name": "responsePart"},
                    }
                    await connection.send_json(mark_event)
                    mark_queue.append("responsePart")

            await asyncio.gather(receive_from_twilio(), send_to_twilio())

    except WebSocketDisconnect:
        print("WebSocket disconnected by the client.")
    except Exception as e:
        print(f"Unexpected error in media stream: {e}")
    finally:
        await websocket.close()


def load_metadata(type: str):
    module_name = f"usecases.{type}.config"
    module = importlib.import_module(module_name)

    global INTRO
    global INSTRUCTIONS
    global GREETING_TEXT
    global VOICE
    global ADVANCED_SETTINGS
    global TOOLS_SCHEMA
    global TOOLS

    INTRO = module.INTRO_TEXT
    INSTRUCTIONS = module.SYSTEM_INSTRUCTIONS
    GREETING_TEXT = module.GREETING_TEXT
    VOICE = module.VOICE
    ADVANCED_SETTINGS = module.ADVANCED_SETTINGS
    TOOLS_SCHEMA = module.TOOLS_SCHEMA
    TOOLS = module.TOOLS


async def send_conversation_item(ws, text, is_last_response_active=False):
    """Send initial conversation item if AI talks first."""
    if is_last_response_active:
        print(
            "Skipping conversation item creation as last response is active. Text:",
            text,
        )
        return

    await ws.send(
        json.dumps(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )
    )

    await ws.send(json.dumps({"type": "response.create"}))


async def initialize_session(ws):
    """Control initial session with OpenAI."""
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": ADVANCED_SETTINGS["turn_detection"]
            or {"type": "server_vad"},
            "input_audio_format": ADVANCED_SETTINGS["input_audio_format"]
            or "g711_ulaw",
            "output_audio_format": ADVANCED_SETTINGS["output_audio_format"]
            or "g711_ulaw",
            "voice": VOICE,
            "instructions": INSTRUCTIONS,
            "modalities": ADVANCED_SETTINGS["modalities"] or ["text", "audio"],
            "temperature": ADVANCED_SETTINGS["temperature"] or 0.8,
            "tools": TOOLS_SCHEMA,
            "tool_choice": "auto",
        },
    }
    print("Sending session update:", json.dumps(session_update))
    await ws.send(json.dumps(session_update))

    # Uncomment the next line to have the AI speak first
    await send_conversation_item(ws, GREETING_TEXT)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=PORT, log_level="info")
