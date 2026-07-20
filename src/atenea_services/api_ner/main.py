import uvicorn

# ======================================================
# =====         RUN REST API WITH UVICORN          =====
# ======================================================
if __name__ == "__main__":
    uvicorn.run("src.api:app")
