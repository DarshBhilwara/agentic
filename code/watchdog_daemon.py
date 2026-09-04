import os, time, requests
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

API = "http://api-gateway:8000/tasks"
HEADERS = {"X-API-Key": "corp-secret-token-123"}

class IngestionHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory: return
        filepath = event.src_path
        time.sleep(1)
        
        with open(filepath, 'r') as f:
            prompt = f.read()
        
        res = requests.post(API, params={"prompt": prompt}, headers=HEADERS).json()
        task_id = res["task_id"]
        
        while True:
            status = requests.get(f"{API}/{task_id}", headers=HEADERS).json()
            if status["status"] == "completed":
                with open(f"/workspace/processed/{os.path.basename(filepath)}.out", "w") as out:
                    out.write(status["result"])
                os.remove(filepath)
                break
            elif status["status"] == "failed":
                os.remove(filepath)
                break
            time.sleep(2)
            
observer = Observer()
observer.schedule(IngestionHandler(), "/workspace/incoming", recursive=False)
observer.start()
print("Zero-touch ingestion active on /workspace/incoming...")
try:
    while True: time.sleep(1)
except KeyboardInterrupt:
    observer.stop()
observer.join()
