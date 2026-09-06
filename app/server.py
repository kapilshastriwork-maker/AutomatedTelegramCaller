import uvicorn
from fastapi import FastAPI

app = FastAPI(title="ATC Backend")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run("app.server:app", host="127.0.0.1", port=8000)
