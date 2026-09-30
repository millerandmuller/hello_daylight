from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="Hello Daylight")


@app.get("/status")
def status():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def home():
    return (
        "<html><body>"
        "<h1>Hello Daylight</h1>"
        "<p>Throw in your project link in the evening. "
        "Wake up to five openings ready to sign.</p>"
        "<p><em>Nothing is live here yet.</em></p>"
        "</body></html>"
    )
