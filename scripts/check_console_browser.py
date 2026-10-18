#!/usr/bin/env python3
"""Local Firefox smoke checks using an isolated profile and fake Redis. No cluster."""
import asyncio
import base64
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
APP = '''import json
import fakeredis
import gateway
from telemetry_ledger import TelemetryLedger
r = fakeredis.FakeRedis()
gateway.r = r
gateway.ledger = TelemetryLedger(r)
l = gateway.ledger
l.start_run("browser-test-run", **{"user.id": "browser-test-user", "program_id": "test-program"})
l.save_summary("browser-test-run", {"program_id": "test-program", "task_status": "Success", "inference_steps": 1, "task_completion_time_ms": 125})
l.save_step("browser-test-run", {"step_index": 1, "step_status": "Success", "tools": [{"tool_name": "read_file", "tool_call_id": "test-call", "tool_status": "Success", "tool_latency_ms": 2}]})
l.emit("tool.completed", run_id="browser-test-run", attributes={"step_index": 1, "tool_name": "read_file", "tool_call_id": "test-call"}, payload={"result": "PRIVATE-TEST-CONTENT"})
r.rpush("kernel:run:browser-test-run", json.dumps({"type": "exit", "syscall_id": 0, "pid": 100, "tid": 101, "tool_call_id": "test-call", "tool_name": "read_file", "ret": 8, "duration_ns": 2000}))
r.hset("kernel:run:browser-test-run:status", mapping={"events": 1, "node": "test-node", "perf_lost": 0})
l.finish_run("browser-test-run", "completed")
app = gateway.app
'''


def wait_http(url, process, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Preview process exited")
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(.1)
    raise TimeoutError(url)


async def check(output):
    import websockets
    checks = []
    with tempfile.TemporaryDirectory(prefix="telemetry-browser-") as temp:
        temp = Path(temp)
        (temp / "console_browser_app.py").write_text(APP)
        profile = temp / "profile"
        profile.mkdir()
        (profile / "user.js").write_text('user_pref("browser.shell.checkDefaultBrowser", false);\nuser_pref("datareporting.policy.dataSubmissionEnabled", false);\n')
        env = dict(os.environ)
        env.update({"PYTHONPATH": os.pathsep.join([str(ROOT / "code"), str(temp)]),
                    "AGENT_API_KEYS": json.dumps({"browser-local-test": "browser-test-user"}),
                    "CONSOLE_K8S_DISCOVERY": "false", "PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION": "python",
                    "AGENT_PROGRAM_TELEMETRY_ENABLED": "true", "AGENT_SEMANTIC_TELEMETRY_ENABLED": "true"})
        for key in list(env):
            if key.startswith("OTEL_EXPORTER_"):
                env.pop(key)
        processes = []
        with (temp / "process.log").open("w+") as log:
            try:
                import sys
                server = subprocess.Popen([sys.executable, "-m", "uvicorn", "console_browser_app:app", "--host", "127.0.0.1", "--port", "18765"], cwd=ROOT, env=env, stdout=log, stderr=log, start_new_session=True)
                processes.append(server)
                await asyncio.to_thread(wait_http, "http://127.0.0.1:18765/dashboard/", server)
                browser = subprocess.Popen([shutil.which("firefox"), "--headless", "--no-remote", "--profile", str(profile), "--remote-debugging-port", "19222", "about:blank"], stdout=log, stderr=log, start_new_session=True)
                processes.append(browser)
                socket = None
                for _ in range(100):
                    try:
                        socket = await websockets.connect("ws://127.0.0.1:19222/session", open_timeout=1)
                        break
                    except OSError:
                        await asyncio.sleep(.1)
                if socket is None:
                    raise RuntimeError("Firefox remote endpoint unavailable")
                async with socket:
                    sequence = 0
                    async def command(method, params):
                        nonlocal sequence
                        sequence += 1
                        await socket.send(json.dumps({"id": sequence, "method": method, "params": params}))
                        while True:
                            message = json.loads(await asyncio.wait_for(socket.recv(), 15))
                            if message.get("id") == sequence:
                                if message.get("type") == "error":
                                    raise RuntimeError(message)
                                return message["result"]
                    await command("session.new", {"capabilities": {}})
                    context = (await command("browsingContext.create", {"type": "tab"}))["context"]
                    async def evaluate(expression):
                        result = await command("script.evaluate", {"expression": expression, "target": {"context": context}, "awaitPromise": True})
                        if result.get("type") == "exception":
                            raise RuntimeError(result)
                        value = result["result"]
                        return value.get("value")
                    async def assertion(expression, label):
                        if not await evaluate(expression):
                            output.mkdir(parents=True, exist_ok=True)
                            shot = await command("browsingContext.captureScreenshot", {"context": context})
                            (output / "console-failure.png").write_bytes(base64.b64decode(shot["data"]))
                            print(await evaluate("JSON.stringify([...document.querySelectorAll('body *')].filter(n=>n.getBoundingClientRect().right>innerWidth+1).map(n=>({tag:n.tagName,id:n.id,class:n.className,right:n.getBoundingClientRect().right,width:n.getBoundingClientRect().width})).slice(0,20))"))
                            raise AssertionError(label)
                        checks.append(label)
                    async def until(expression):
                        for _ in range(100):
                            if await evaluate(expression):
                                return
                            await asyncio.sleep(.05)
                        raise AssertionError(f"Timed out: {expression}")
                    await command("browsingContext.setViewport", {"context": context, "viewport": {"width": 1440, "height": 1000}})
                    await command("browsingContext.navigate", {"context": context, "url": "http://127.0.0.1:18765/dashboard/", "wait": "complete"})
                    await assertion("document.querySelector('#connection-label').textContent === 'Not connected' && getComputedStyle(document.querySelector('.sidebar')).backgroundColor !== 'rgba(0, 0, 0, 0)'", "Offline page and CSS load")
                    await evaluate("document.querySelector('#layer-cards input').click()")
                    await assertion("document.querySelector('#cfg-program_enabled').checked === false", "Overview toggle synchronizes collection form")
                    await evaluate("document.querySelector('#connect-button').click(); document.querySelector('#demo-button').click()")
                    await assertion("document.querySelector('#mode-banner').textContent.includes('DEMO MODE')", "Demo is explicitly labelled")
                    await evaluate("document.querySelector('[data-page=devices]').click()")
                    await assertion("document.querySelector('#nodes-table').textContent.includes('Ready')", "Demo device readiness renders")
                    await command("browsingContext.navigate", {"context": context, "url": "http://127.0.0.1:18765/dashboard/#overview", "wait": "complete"})
                    await evaluate("document.querySelector('#connect-button').click(); document.querySelector('#api-token').value='browser-local-test'; document.querySelector('#connection-form').requestSubmit()")
                    await until("document.querySelector('#connection-label').textContent === 'Connected'")
                    await assertion("document.querySelector('#overview-runs').textContent.includes('browser-test-run')", "Authenticated gateway data loads")
                    await evaluate("document.querySelector('[data-page=collection]').click(); document.querySelector('#config-form').requestSubmit()")
                    await until("!document.querySelector('#preview-panel').classList.contains('hidden')")
                    await assertion("document.querySelector('#preview-files').textContent.includes('inference-node.yaml')", "Validated preview includes inference manifest")
                    await evaluate("window.__baseAnchorClick=HTMLAnchorElement.prototype.click; HTMLAnchorElement.prototype.click=function(){if(this.download.endsWith('.yaml')){fetch(this.href).then(r=>r.text()).then(text=>window.__manifestPayload=text)}else window.__baseAnchorClick.call(this)}; document.querySelector('#download-manifests').click()")
                    await until("typeof window.__manifestPayload === 'string'")
                    import yaml
                    manifests = list(yaml.safe_load_all(await evaluate("window.__manifestPayload")))
                    if not manifests or not all(isinstance(item, dict) and 'kind' in item for item in manifests):
                        raise AssertionError("Downloaded manifest YAML must contain only Kubernetes objects")
                    if manifests[0]['kind'] != 'Namespace':
                        raise AssertionError("Namespace must precede namespace-bound resources")
                    checks.append("Downloaded YAML parses and starts with namespace")
                    await evaluate("HTMLAnchorElement.prototype.click=window.__baseAnchorClick")
                    await evaluate("document.querySelector('#cfg-semantic_enabled').click()")
                    await assertion("document.querySelector('#preview-panel').classList.contains('hidden')", "Editing draft invalidates validated preview")
                    await evaluate("document.querySelector('[data-page=runs]').click()")
                    await assertion("!document.querySelector('#runs-list input[type=checkbox]') || !document.querySelector('#runs-list input[type=checkbox]').checked", "Run selections begin unchecked")
                    await evaluate("document.querySelector('#runs-list .run-row').click()")
                    await until("document.querySelector('#run-detail').textContent.includes('browser-test-run')")
                    await evaluate("Array.from(document.querySelectorAll('#run-detail [role=tab]')).find(n=>n.textContent==='Kernel').click()")
                    await assertion("document.querySelector('#run-detail').textContent.includes('test-call')", "Kernel view shows tool/process correlation")
                    await evaluate("window.__downloads=[]; window.__anchorClick=HTMLAnchorElement.prototype.click; HTMLAnchorElement.prototype.click=function(){if(this.download){const filename=this.download;fetch(this.href).then(r=>r.text()).then(content=>window.__downloads.push({filename,content}));}else window.__anchorClick.call(this)}; document.querySelector('[data-page=reports]').click(); document.querySelector('#report-run-list input').click(); document.querySelector('#generate-report').click()")
                    await until("window.__downloads.length === 1")
                    await assertion("window.__downloads[0].content.includes('browser-test-run') && !window.__downloads[0].content.includes('PRIVATE-TEST-CONTENT')", "Live report downloads bounded safe data")
                    await evaluate("window.__originalFetch=window.fetch; window.fetch=async function(url,options){if(String(url).endsWith('/console/reports')) return {ok:true,headers:new Headers({'content-disposition':'attachment; filename=test.json'}),blob:()=>new Promise(resolve=>window.__resolveReport=resolve)};return window.__originalFetch.call(this,url,options)}; document.querySelector('#generate-report').click()")
                    await until("typeof window.__resolveReport === 'function'")
                    await evaluate("document.querySelector('#connect-button').click(); window.__resolveReport(new Blob(['PRIVATE-STALE-REPORT'])); window.fetch=window.__originalFetch")
                    await asyncio.sleep(.1)
                    await assertion("window.__downloads.length === 1", "Disconnect blocks a late report download")
                    await assertion("!document.querySelector('#run-detail').textContent.includes('PRIVATE-TEST-CONTENT') && !document.querySelector('#overview-runs').textContent.includes('browser-test-run')", "Disconnect clears observed data")
                    await assertion("!localStorage.getItem('telemetry-console:draft:v1')?.includes('browser-local-test')", "API token is not persisted")
                    await command("browsingContext.setViewport", {"context": context, "viewport": {"width": 390, "height": 844}})
                    await asyncio.sleep(.25)
                    for page in ("overview", "collection", "devices", "runs", "reports"):
                        await evaluate(f"document.querySelector('[data-page={page}]').click()")
                        await assertion("document.documentElement.scrollWidth <= window.innerWidth", f"Mobile {page} layout has no page overflow")
                    await evaluate("document.querySelector('[data-page=overview]').click()")
                    output.mkdir(parents=True, exist_ok=True)
                    shot = await command("browsingContext.captureScreenshot", {"context": context})
                    (output / "console-mobile.png").write_bytes(base64.b64decode(shot["data"]))
                    await command("browsingContext.setViewport", {"context": context, "viewport": {"width": 1440, "height": 1000}})
                    await evaluate("document.querySelector('#connect-button').click(); document.querySelector('#demo-button').click(); document.querySelector('[data-page=overview]').click(); document.activeElement.blur()")
                    shot = await command("browsingContext.captureScreenshot", {"context": context})
                    (output / "console-desktop.png").write_bytes(base64.b64decode(shot["data"]))
                    await command("session.end", {})
            except Exception:
                log.flush()
                print((temp / "process.log").read_text()[-3000:])
                raise
            finally:
                for process in reversed(processes):
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait()
    print(json.dumps({"checks": checks, "screenshots": str(output)}, indent=2))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("/tmp/telemetry-console-preview"))
    args = parser.parse_args()
    if not shutil.which("firefox"):
        raise SystemExit("Firefox is required for this optional browser check")
    asyncio.run(check(args.output))
