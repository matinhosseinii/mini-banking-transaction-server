"""Run the Coordinator with environment-backed Uvicorn settings."""

import uvicorn


def main() -> None:
    uvicorn.run("coordinator.app:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
