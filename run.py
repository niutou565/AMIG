"""启动入口"""
import os
import uvicorn
from app.main import app

if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    workers = int(os.getenv("WORKERS", "1"))
    uvicorn.run("app.main:app", host=host, port=port, reload=False, workers=workers)