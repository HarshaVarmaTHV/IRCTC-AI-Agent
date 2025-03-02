import asyncio
import base64
import io
import os
import sys
import traceback
import cv2
import pyaudio
import PIL.Image
import mss
import argparse
import requests  # used for external API calls
import threading
import queue
import tkinter as tk
from tkinter import simpledialog, messagebox

from google import genai

# Global queues for GUI and async communication
gui_to_async = queue.Queue()   # Messages from GUI (user input) to async loop
async_to_gui = queue.Queue()   # Messages from async loop (agent responses) to GUI

# For Python versions < 3.11, add missing TaskGroup and ExceptionGroup.
if sys.version_info < (3, 11):
    import taskgroup, exceptiongroup
    asyncio.TaskGroup = taskgroup.TaskGroup
    asyncio.ExceptionGroup = exceptiongroup.ExceptionGroup

# --- Setup tools and configuration for function calling ---
class FunctionTool:
    def __init__(self, function_declarations):
        self.function_declarations = function_declarations

check_ticket_status_tool = FunctionTool([
    {
        "name": "ticket_IRCTC_status",
        "description": "Retrieve ticket status for a given ticket_pnr. You can also tell various details about the ticket",
        "parameters": {
            "type": "object",
            "properties": {
                "ticket_pnr": {
                    "type": "string",
                    "description": "The ticket PNR to check status for."
                }
            },
            "required": ["ticket_pnr"]
        }
    }
])

CONFIG = {
    "system_instruction": "Your name is IRCTC Train AI Agent. Here to help you with your tickets and Train details. ",
    "generation_config": {
        "response_modalities": ["AUDIO"],
        "tools": [ check_ticket_status_tool],
    }
}

MODEL = "models/gemini-2.0-flash-exp"
DEFAULT_MODE = "none"  # default video mode

# Client will be created later after obtaining the API key.
client = None

# Audio settings
FORMAT = pyaudio.paInt16
CHANNELS = 1
SEND_SAMPLE_RATE = 16000
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE = 1024
pya = pyaudio.PyAudio()

# --- Function calling helper functions ---
def check_IRCTC_ticket_status(ticket_pnr):
    url = "https://asia-south1-my-first-project.cloudfunctions.net/ticket-details"
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Basic adfdfdfds3242r4324342sdfdfa"
    }
    payload = {"ticket": ticket_pnr}
    print(f"Calling API for ticket status with ticket_pnr: {ticket_pnr}")
    try:
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()
        print(f"API call successful. Response: {response.text}")
        return response.json()
    except Exception as e:
        print(f"API call failed: {str(e)}")
        return {"error": str(e)}

def handle_server_content(server_content):
    model_turn = server_content.model_turn
    if model_turn:
        for part in model_turn.parts:
            if part.executable_code is not None:
                print('-------------------------------')
                print(f'``` python\n{part.executable_code.code}\n```')
                print('-------------------------------')
            if part.code_execution_result is not None:
                print('-------------------------------')
                print(f'```\n{part.code_execution_result.output}\n```')
                print('-------------------------------')

async def handle_tool_call(session, tool_call):
    for fc in tool_call.function_calls:
        if fc.name == "ticket_IRCTC_status":
            ticket_pnr = fc.args.get("ticket_pnr")
            print(f"Handling tool call: {fc.name} with ticket_pnr: {ticket_pnr}")
            response_json = check_IRCTC_ticket_status(ticket_pnr)
            tool_response = genai.types.LiveClientToolResponse(
                function_responses=[
                    genai.types.FunctionResponse(
                        name=fc.name,
                        id=fc.id,
                        response=response_json,
                    )
                ]
            )
        else:
            tool_response = genai.types.LiveClientToolResponse(
                function_responses=[
                    genai.types.FunctionResponse(
                        name=fc.name,
                        id=fc.id,
                        response={'result': 'ok'},
                    )
                ]
            )
        print(f"Sending tool response: {tool_response}")
        await session.send(input=tool_response)

# --- Updated AudioLoop class with GUI integration ---
class AudioLoop:
    def __init__(self, video_mode=DEFAULT_MODE):
        self.video_mode = video_mode
        self.audio_in_queue = None
        self.out_queue = None
        self.session = None

    async def process_gui_messages(self):
        """Poll messages from the GUI and send them to the agent."""
        while True:
            try:
                msg = gui_to_async.get_nowait()
            except queue.Empty:
                msg = None
            if msg is not None:
                if msg.lower() == "q":
                    break
                await self.session.send(input=msg, end_of_turn=True)
            await asyncio.sleep(0.1)

    def _get_frame(self, cap):
        ret, frame = cap.read()
        if not ret:
            return None
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = PIL.Image.fromarray(frame_rgb)
        img.thumbnail([1024, 1024])
        image_io = io.BytesIO()
        img.save(image_io, format="jpeg")
        image_io.seek(0)
        mime_type = "image/jpeg"
        image_bytes = image_io.read()
        return {"mime_type": mime_type, "data": base64.b64encode(image_bytes).decode()}

    async def get_frames(self):
        cap = await asyncio.to_thread(cv2.VideoCapture, 0)
        while True:
            frame = await asyncio.to_thread(self._get_frame, cap)
            if frame is None:
                break
            await asyncio.sleep(1.0)
            await self.out_queue.put(frame)
        cap.release()

    def _get_screen(self):
        with mss.mss() as sct:
            monitor = sct.monitors[0]
            img = sct.grab(monitor)
            mime_type = "image/jpeg"
            image_bytes = mss.tools.to_png(img.rgb, img.size)
            pil_img = PIL.Image.open(io.BytesIO(image_bytes))
            image_io = io.BytesIO()
            pil_img.save(image_io, format="jpeg")
            image_io.seek(0)
            image_bytes = image_io.read()
            return {"mime_type": mime_type, "data": base64.b64encode(image_bytes).decode()}

    async def get_screen(self):
        while True:
            frame = await asyncio.to_thread(self._get_screen)
            if frame is None:
                break
            await asyncio.sleep(1.0)
            await self.out_queue.put(frame)

    async def send_realtime(self):
        while True:
            msg = await self.out_queue.get()
            await self.session.send(input=msg)

    async def listen_audio(self):
        mic_info = pya.get_default_input_device_info()
        self.audio_stream = await asyncio.to_thread(
            pya.open,
            format=FORMAT,
            channels=CHANNELS,
            rate=SEND_SAMPLE_RATE,
            input=True,
            input_device_index=mic_info["index"],
            frames_per_buffer=CHUNK_SIZE,
        )
        kwargs = {"exception_on_overflow": False} if __debug__ else {}
        while True:
            data = await asyncio.to_thread(self.audio_stream.read, CHUNK_SIZE, **kwargs)
            await self.out_queue.put({"data": data, "mime_type": "audio/pcm"})

    async def receive_audio(self):
        """
        Modified receive loop: text responses are sent to the GUI.
        """
        while True:
            turn = self.session.receive()
            async for response in turn:
                if response.data:
                    self.audio_in_queue.put_nowait(response.data)
                elif response.text:
                    # Instead of printing to console, put text in the async_to_gui queue.
                    async_to_gui.put(response.text)
                elif response.server_content:
                    handle_server_content(response.server_content)
                elif response.tool_call:
                    await handle_tool_call(self.session, response.tool_call)
            while not self.audio_in_queue.empty():
                self.audio_in_queue.get_nowait()

    async def play_audio(self):
        stream = await asyncio.to_thread(
            pya.open,
            format=FORMAT,
            channels=CHANNELS,
            rate=RECEIVE_SAMPLE_RATE,
            output=True,
        )
        while True:
            bytestream = await self.audio_in_queue.get()
            await asyncio.to_thread(stream.write, bytestream)

    async def run(self):
        try:
            async with client.aio.live.connect(model=MODEL, config=CONFIG) as session, asyncio.TaskGroup() as tg:
                self.session = session
                self.audio_in_queue = asyncio.Queue()
                self.out_queue = asyncio.Queue(maxsize=5)
                # Instead of a command-line prompt, process GUI messages.
                tg.create_task(self.process_gui_messages())
                tg.create_task(self.send_realtime())
                tg.create_task(self.listen_audio())
                if self.video_mode == "camera":
                    tg.create_task(self.get_frames())
                elif self.video_mode == "screen":
                    tg.create_task(self.get_screen())
                tg.create_task(self.receive_audio())
                tg.create_task(self.play_audio())
                await asyncio.sleep(0)  # Ensure tasks start
        except asyncio.CancelledError:
            pass
        except Exception as e:
            traceback.print_exception(e)
            self.audio_stream.close()

# --- GUI Functions ---
def ask_video_mode():
    mode_window = tk.Toplevel()
    mode_window.title("Select Video Mode")
    mode_var = tk.StringVar(value="none")
    tk.Label(mode_window, text="Select video mode:").pack(pady=10)
    for text, mode in [("None", "none"), ("Screen", "screen"), ("Camera", "camera")]:
        tk.Radiobutton(mode_window, text=text, variable=mode_var, value=mode).pack(anchor=tk.W)
    tk.Button(mode_window, text="OK", command=mode_window.destroy).pack(pady=10)
    mode_window.grab_set()
    mode_window.wait_window()
    return mode_var.get()

def start_chat_gui(root):
    """Creates the main chat window with a conversation box and input area."""
    chat_window = tk.Toplevel(root)
    chat_window.title("Chat with IRCTC AI Agent")
    
    chat_text = tk.Text(chat_window, wrap='word', height=20, width=60)
    chat_text.pack(padx=10, pady=10, fill='both', expand=True)
    
    input_frame = tk.Frame(chat_window)
    input_frame.pack(padx=10, pady=10, fill='x')
    input_entry = tk.Entry(input_frame, width=50)
    input_entry.pack(side='left', padx=(0, 10), fill='x', expand=True)
    
    def send_message(event=None):
        msg = input_entry.get().strip()
        if msg:
            gui_to_async.put(msg)
            chat_text.insert(tk.END, "You: " + msg + "\n")
            chat_text.see(tk.END)
            input_entry.delete(0, tk.END)
    send_button = tk.Button(input_frame, text="Send", command=send_message)
    send_button.pack(side='left')
    input_entry.bind("<Return>", send_message)
    
    def poll_async_messages():
        while not async_to_gui.empty():
            try:
                msg = async_to_gui.get_nowait()
                chat_text.insert(tk.END, "Agent: " + msg + "\n")
                chat_text.see(tk.END)
            except queue.Empty:
                break
        chat_window.after(100, poll_async_messages)
    
    poll_async_messages()

# --- Main Application Startup ---
if __name__ == "__main__":
    # Initialize Tkinter root and hide the main window.
    root = tk.Tk()
    root.withdraw()

    # Prompt user for GOOGLE_API_KEY.
    api_key = simpledialog.askstring("API Key", "Please enter your GOOGLE_API_KEY:")
    if not api_key:
        messagebox.showerror("Error", "No API key provided. Exiting.")
        sys.exit(1)
    os.environ["GOOGLE_API_KEY"] = api_key

    # Prompt for video mode.
    video_mode = ask_video_mode()
    print(f"Video mode selected: {video_mode}")

    # Initialize the client after collecting API key.
    client = genai.Client(api_key=api_key, http_options={"api_version": "v1alpha"})

    # Create the main chat window.
    root.deiconify()  # Show the root window (or create a new one)
    start_chat_gui(root)

    # Run the AudioLoop in a separate thread.
    def run_async():
        asyncio.run(AudioLoop(video_mode=video_mode).run())
    
    async_thread = threading.Thread(target=run_async, daemon=True)
    async_thread.start()
    
    # Start the Tkinter mainloop.
    root.mainloop()
