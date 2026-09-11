import argparse

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="OpenAI-compatible LLM gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4000)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    uvicorn.run(
        "llm_gateway.app:create_app_from_env", factory=True, host=args.host, port=args.port, workers=args.workers
    )


if __name__ == "__main__":
    main()
