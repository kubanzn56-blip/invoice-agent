from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler
from agent import uruchom_agenta
import uvicorn
import os

app = FastAPI(title="Invoice Agent")
scheduler = BackgroundScheduler()

INTERVAL_MIN = int(os.getenv("INTERVAL_MIN", "5"))


@app.on_event("startup")
def start():
    scheduler.add_job(uruchom_agenta, "interval", minutes=INTERVAL_MIN)
    scheduler.start()
    print(f"Invoice Agent uruchomiony — sprawdza maile co {INTERVAL_MIN} minut.")
    uruchom_agenta()


@app.on_event("shutdown")
def stop():
    scheduler.shutdown()


@app.get("/")
def root():
    return {"status": "dziala", "info": "Invoice Agent aktywny"}


@app.get("/run")
def run_now():
    uruchom_agenta()
    return {"status": "wykonano"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)