import uvicorn
from settings import UVICORN_WORKERS

# ======================================================
# =====         RUN REST API WITH UVICORN          =====
# ======================================================
if __name__ == "__main__":
    uvicorn.run("src.api:app", host='0.0.0.0', workers=UVICORN_WORKERS)
