import redis, os, re, requests
from bs4 import BeautifulSoup
from openai import OpenAI

r = redis.Redis(host='redis-service', port=6379, db=0)
client = OpenAI(base_url="http://vllm-service:8000/v1", api_key="EMPTY")

def process(prompt):
    urls = re.findall(r'(https?://[^\s]+)', prompt)
    context = ""
    if urls:
        try:
            resp = requests.get(urls[0], timeout=10)
            soup = BeautifulSoup(resp.text, 'html.parser')
            context = f"Web Context: {soup.get_text(separator=' ', strip=True)[:3000]}\n\n"
        except Exception as e:
            context = f"Scraping failed: {e}\n\n"
    
    full_prompt = context + prompt
    resp = client.chat.completions.create(
        model="llama3",
        messages=[{"role": "user", "content": full_prompt}]
    )
    return resp.choices[0].message.content

print("Agent Worker Booted. Listening to queue...")
while True:
    task_data = r.brpop("task_queue", timeout=0)
    if task_data:
        task_id = task_data[1].decode()
        r.hset(f"task:{task_id}", "status", "processing")
        task = r.hgetall(f"task:{task_id}")
        
        try:
            result = process(task[b'prompt'].decode())
            user = task[b'user'].decode()
            
            user_dir = f"/workspace/users/{user}"
            os.makedirs(user_dir, exist_ok=True)
            with open(f"{user_dir}/{task_id}.txt", "w") as f:
                f.write(result)
            
            r.hset(f"task:{task_id}", mapping={"status": "completed", "result": result})
        except Exception as e:
            r.hset(f"task:{task_id}", mapping={"status": "failed", "error": str(e)})
