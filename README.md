# Walarcon Voice Assistant

This project contains a phone assistant that interacts with Groq/OpenAI and Deepgram.

## Streaming GPT Responses

Set the environment variable `USE_GPT_STREAMING=true` to enable streaming mode. In this
mode `tw_utils.process_gpt_response` uses the generator `generate_openai_response_streaming`
from `aiagent.py` to speak partial responses as tokens arrive.

