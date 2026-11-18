import uvicorn


if __name__ == "__main__":
    uvicorn.run("lucima_bridge.web:app", host="127.0.0.1", port=8092, workers=1,
                access_log=False, proxy_headers=False, timeout_graceful_shutdown=110)
