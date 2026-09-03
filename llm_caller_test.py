import os
import json
import requests
from dotenv import load_dotenv

# Load .env file in the current directory
load_dotenv()

API_KEY = os.getenv("TAMU_API_KEY")
BASE_URL = os.getenv("TAMU_BASE_URL")   # full endpoint, e.g. https://chat-api.tamu.ai/openai/chat/completions
MODEL    = os.getenv("TAMU_MODEL")

if not API_KEY or not BASE_URL or not MODEL:
    raise RuntimeError(
        "Missing TAMU_API_KEY, TAMU_BASE_URL, or TAMU_MODEL in .env"
    )

def main():
    url = BASE_URL.strip()
    print("Using URL:", url)
    print("Using model:", MODEL)

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "user", "content": "Say hello from the validation flow!"}
        ],
        "max_tokens": 2048,
        "temperature": 1,
        # Gateway is clearly streaming; we can leave this out or set True
        # "stream": True,
    }

    resp = requests.post(url, headers=headers, data=json.dumps(payload))
    print("Status code:", resp.status_code)

    if resp.status_code != 200:
        print("Error body:", resp.text)
        return

    body = resp.text
    print("\n===== RAW RESPONSE BODY =====")
    print(body)
    print("===== END RAW RESPONSE =====\n")

    # Parse SSE-style streaming chunks
    full_reply = []

    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue

        chunk = line[len("data:"):].strip()

        # Skip DONE markers and empty chunks
        if not chunk or chunk == "[DONE]":
            continue

        try:
            data = json.loads(chunk)
        except Exception as e:
            # If some weird line shows up, just skip it
            # print("Skipping non-JSON chunk:", repr(chunk), "error:", repr(e))
            continue

        try:
            delta = data["choices"][0]["delta"]
            piece = delta.get("content", "")
            if piece:
                full_reply.append(piece)
        except Exception:
            # If the structure is different for some chunks (e.g. finish_reason),
            # we just ignore those.
            continue

    final_text = "".join(full_reply).strip()

    if final_text:
        print("LLM reply (assembled):", final_text)
    else:
        print("Could not assemble reply text; see raw response above.")

if __name__ == "__main__":
    main()