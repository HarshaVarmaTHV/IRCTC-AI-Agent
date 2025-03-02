# IRCTC Train Ticket Status Agent

A multimodal AI agent built using Google’s Gemini 2.0 Live model. This application listens, talks, and even “sees” your screen to guide you with train ticket information and check PNR status using a Google Cloud Function.

## Features

- **Multimodal Interaction**: Uses audio input/output and visual screen capture.
- **Function Calling**: Integrates with a cloud function to check IRCTC ticket status.
- **Easy-to-Use GUI**: Built with Tkinter for interactive chat.
- **Powered by Gemini 2.0 Live**: Leverages Google’s latest Gemini model for real-time interactions.

## File Structure

IRCTC-AI-Agent/
├── README.md                      # Project documentation
├── requirements.txt               # Python dependencies
├── irctc_agent.py                 # All-in-one application code including gemini 2.0 calling , recieving , ticket status function calling and tkinter simple UI
