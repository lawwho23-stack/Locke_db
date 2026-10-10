"""Real Next.js cookie/proxy acceptance with an isolated PostgreSQL backend."""

import os
import shutil
import socket
import subprocess
import sys
import time
from http.cookies import SimpleCookie
from pathlib import Path

import httpx
import pytest

from tests.conftest import TEST_HMAC_KEY_HEX

WEB = Path(__file__).resolve().parents[1] / "apps" / "web"


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_ready(url, process):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail("Dashboard smoke process exited before becoming ready.")
        try:
            if httpx.get(url, timeout=1).status_code < 500:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.1)
    pytest.fail("Dashboard smoke process did not become ready.")


@pytest.mark.skipif(
    not shutil.which("node") or not (WEB / ".next" / "BUILD_ID").exists(),
    reason="Run npm install && npm run build in apps/web before web acceptance.",
)
def test_real_owner_cookie_and_proxy(test_db_url, workspace, make_agent_client, make_scope):
    other_scope = make_scope("project", "zz-onboarding-scope")
    api_port, web_port = free_port(), free_port()
    api_url, web_url = f"http://127.0.0.1:{api_port}", f"http://127.0.0.1:{web_port}"
    env = {
        **os.environ,
        "DATABASE_URL": test_db_url,
        "MEMORY_HMAC_KEY": TEST_HMAC_KEY_HEX,
        "MEMORY_API_URL": api_url,
        "WEB_ORIGIN": web_url,
        "SESSION_SECRET": "dashboard-test-secret-only-" * 3,
        "PROVIDER_DAILY_BUDGET_USD": "0",
        "PROVIDER_API_KEY": "",
        "REDIS_REST_URL": "",
        "REDIS_REST_TOKEN": "",
    }
    processes = []
    try:
        api = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "memory_platform.api.app:create_default_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(api_port),
                "--log-level",
                "error",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        processes.append(api)
        wait_ready(api_url + "/health", api)
        web = subprocess.Popen(
            [
                "node",
                "node_modules/next/dist/bin/next",
                "start",
                "--hostname",
                "127.0.0.1",
                "--port",
                str(web_port),
            ],
            cwd=WEB,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        processes.append(web)
        wait_ready(web_url, web)
        agent = make_agent_client([workspace.personal_scope_id], ["memory:read"])
        with httpx.Client(base_url=web_url, headers={"Origin": web_url}) as client:
            assert client.get("/").status_code == 200
            assert client.get("/api/session").status_code == 401
            assert client.post("/api/session", json={"token": agent.token}).status_code == 403
            owner = client.post("/api/session", json={"token": workspace.owner_token})
            assert owner.status_code == 200
            cookie = owner.headers["set-cookie"]
            parsed = SimpleCookie()
            parsed.load(cookie)
            assert parsed["memory_owner"]["httponly"]
            assert parsed["memory_owner"]["samesite"].lower() == "strict"
            # Owner sessions have no time expiry: the cookie must persist
            # across refresh, tab close, and browser restart.
            assert int(parsed["memory_owner"]["max-age"]) > 3600
            assert workspace.owner_token not in cookie and workspace.owner_token not in owner.text
            summary = client.get("/api/proxy/dashboard/summary")
            assert summary.status_code == 200
            assert summary.json()["actor_kind"] == "owner"
            assert summary.headers["cache-control"] == "no-store"
            assert client.get("/api/session").headers["cache-control"] == "no-store"
            created = client.post(
                "/api/proxy/credentials",
                json={
                    "display_name": "Dashboard scoped agent",
                    "grants": [
                        {
                            "scope_id": str(workspace.personal_scope_id),
                            "capabilities": ["memory:read"],
                        }
                    ],
                },
            )
            assert created.status_code == 201
            issued_token = created.json()["token"]
            assert workspace.owner_token not in created.text
            inventory = client.get("/api/proxy/dashboard/connections")
            assert inventory.status_code == 200
            assert issued_token not in inventory.text
            assert "token_hash" not in inventory.text
            credential_id = created.json()["id"]
            assert (
                client.delete(
                    f"/api/proxy/credentials/{credential_id}",
                    headers={"Origin": "https://evil.test"},
                ).status_code
                == 403
            )
            assert client.delete(f"/api/proxy/credentials/{credential_id}").status_code == 200
            assert client.post("/api/proxy/skills", json={}).status_code == 404
            assert (
                client.delete("/api/session", headers={"Origin": "https://evil.test"}).status_code
                == 403
            )
            assert client.delete("/api/session").status_code == 200
            assert client.get("/api/proxy/dashboard/summary").status_code == 401
        if os.environ.get("DASHBOARD_BROWSER_ACCEPTANCE") == "1":
            import tempfile

            with httpx.Client(
                base_url=api_url, headers={"Authorization": f"Bearer {workspace.owner_token}"}
            ) as api_client:
                memory_ids = []
                for content in (
                    "Browser primary decision",
                    "Browser linked evidence",
                    "Browser unrelated note",
                ):
                    response = api_client.post(
                        "/v1/memories",
                        json={
                            "scope_id": str(workspace.personal_scope_id),
                            "type": "decision",
                            "content": content,
                            "labels": ["browser"],
                        },
                    )
                    assert response.status_code == 201
                    memory_ids.append(response.json()["id"])
                assert (
                    api_client.post(
                        "/v1/relations", json={"from_id": memory_ids[0], "to_id": memory_ids[1]}
                    ).status_code
                    == 201
                )
            artifacts = Path(tempfile.mkdtemp(prefix="memory-v1-browser-evidence-"))
            browser_acceptance(
                web_url,
                workspace.owner_token,
                artifacts,
                str(workspace.personal_scope_id),
                str(other_scope),
            )
            print(f"Browser evidence: {artifacts}")
    finally:
        for process in reversed(processes):
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def browser_acceptance(web_url, owner_token, artifacts, personal_scope, other_scope):
    """Exercise real Chrome through its DevTools protocol without new dependencies."""
    import base64
    import json
    import tempfile

    from websockets.sync.client import connect

    chrome_path = os.environ.get(
        "DASHBOARD_CHROME_PATH",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    )
    assert Path(chrome_path).is_file(), "Set DASHBOARD_CHROME_PATH for browser acceptance."
    port = free_port()
    with tempfile.TemporaryDirectory(prefix="memory-browser-profile-") as profile:
        chrome = subprocess.Popen(
            [
                chrome_path,
                "--headless=new",
                "--no-first-run",
                "--disable-background-networking",
                "--disable-default-apps",
                "--disable-sync",
                "--remote-allow-origins=*",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            wait_ready(f"http://127.0.0.1:{port}/json/version", chrome)
            page = next(
                page
                for page in httpx.get(f"http://127.0.0.1:{port}/json").json()
                if page.get("type") == "page"
            )
            with connect(page["webSocketDebuggerUrl"], max_size=16 * 1024 * 1024) as connection:
                sequence = 0
                exceptions = []

                def command(method, params=None):
                    nonlocal sequence
                    sequence += 1
                    command_id = sequence
                    connection.send(
                        json.dumps({"id": command_id, "method": method, "params": params or {}})
                    )
                    while True:
                        response = json.loads(connection.recv(timeout=10))
                        if response.get("method") == "Runtime.exceptionThrown":
                            exceptions.append(response["params"])
                        if response.get("id") == command_id:
                            assert "error" not in response, response.get("error")
                            return response.get("result", {})

                def evaluate(expression):
                    result = command(
                        "Runtime.evaluate",
                        {"expression": expression, "returnByValue": True, "awaitPromise": True},
                    )
                    assert "exceptionDetails" not in result, result.get("exceptionDetails")
                    return result.get("result", {}).get("value")

                def wait(expression):
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        if evaluate(expression):
                            return
                        time.sleep(0.1)
                    pytest.fail("Browser acceptance condition timed out: " + expression)

                def click(label):
                    return evaluate(
                        "Array.from(document.querySelectorAll('button'))"
                        ".find(b => b.getAttribute('aria-label') === "
                        + json.dumps(label)
                        + " || b.textContent.trim() === "
                        + json.dumps(label)
                        + ")?.click()"
                    )

                def fill(selector, value):
                    evaluate(
                        "(() => { const input = document.querySelector("
                        + json.dumps(selector)
                        + "); Object.getOwnPropertyDescriptor("
                        "HTMLInputElement.prototype,'value').set.call(input,"
                        + json.dumps(value)
                        + "); input.dispatchEvent(new Event('input',{bubbles:true})); })()"
                    )

                command("Runtime.enable")
                command("Page.enable")
                command(
                    "Emulation.setDeviceMetricsOverride",
                    {"width": 1440, "height": 1000, "deviceScaleFactor": 1, "mobile": False},
                )
                command("Page.navigate", {"url": web_url})
                wait("document.querySelector('input[type=password]') !== null")
                screenshot = command("Page.captureScreenshot", {"format": "png"})["data"]
                (artifacts / "login-desktop.png").write_bytes(base64.b64decode(screenshot))
                fill("input[type=password]", owner_token)
                click("Open workspace")
                wait("document.querySelector('.tabs') !== null")
                assert not evaluate("document.querySelector('[role=alert]')?.textContent")
                fill('input[placeholder="Find stored knowledge"]', "primary")
                wait("document.querySelectorAll('.row').length === 1")
                assert "Browser primary decision" in evaluate("document.body.innerText")
                assert "Browser unrelated note" not in evaluate("document.body.innerText")
                fill('input[placeholder="Find stored knowledge"]', "")
                wait("document.querySelectorAll('.row').length === 3")
                evaluate("document.querySelector('.rows .row').click()")
                wait("document.querySelector('.detail textarea') !== null")
                click("Forget")
                wait("document.querySelector('dialog').open")
                click("Cancel")
                assert evaluate("document.querySelectorAll('.rows .row').length") == 3
                click("Close inspector")
                assert evaluate("document.querySelector('.detail-open') === null")
                screenshot = command("Page.captureScreenshot", {"format": "png"})["data"]
                (artifacts / "memories-desktop.png").write_bytes(base64.b64decode(screenshot))
                for section in ("Tasks", "Approved skills", "Activity"):
                    click(section)
                    wait("document.querySelector('h1')?.textContent === " + json.dumps(section))
                    wait("document.querySelector('.loading') === null")
                    assert not evaluate("document.querySelector('[role=alert]')?.textContent")
                    screenshot = command("Page.captureScreenshot", {"format": "png"})["data"]
                    (artifacts / f"{section.lower().replace(' ', '-')}-desktop.png").write_bytes(
                        base64.b64decode(screenshot)
                    )
                click("Graph")
                wait("document.querySelector('.react-flow__minimap') !== null")
                wait("document.querySelectorAll('.react-flow__node').length >= 1")
                evaluate(
                    "Array.from(document.querySelectorAll('.react-flow__node'))"
                    ".find(n => n.textContent.includes('Browser primary decision')).click()"
                )
                wait("document.querySelector('.detail textarea') !== null")
                click("Expand neighbors")
                wait("document.querySelectorAll('.react-flow__node').length === 3")
                assert "Browser unrelated note" not in evaluate(
                    "document.querySelector('.graph').innerText"
                )
                screenshot = command("Page.captureScreenshot", {"format": "png"})["data"]
                (artifacts / "dashboard-desktop.png").write_bytes(base64.b64decode(screenshot))
                click("Close inspector")
                click("Connections")
                wait("document.querySelector('input[name=name]') !== null")
                assert evaluate("document.querySelector('select').options.length") >= 1
                assert (
                    evaluate(
                        "Array.from(document.querySelectorAll('.capabilities input:checked'))"
                        ".map(i => i.parentElement.textContent.trim()).sort().join(',')"
                    )
                    == "memory:read,memory:write"
                )
                fill("input[name=name]", "Browser scoped agent")
                click("Create scoped credential")
                wait("document.querySelector('.token-reveal code') !== null")
                assert "locke-db-api.vercel.app/mcp" in evaluate(
                    "document.querySelector('.token-reveal').innerText"
                )
                assert "Starter prompt" in evaluate(
                    "document.querySelector('.token-reveal').innerText"
                )
                assert evaluate(
                    "Array.from(document.querySelectorAll('.token-reveal pre'))"
                    ".every(p => !p.textContent.includes("
                    "document.querySelector('.token-reveal > code').textContent))"
                )
                assert evaluate(
                    "Object.keys(localStorage).length === 0 && "
                    "Object.keys(sessionStorage).length === 0"
                )
                issued = evaluate("document.querySelector('.token-reveal code').textContent")
                assert issued.startswith("mem_") and issued != owner_token
                assert not evaluate("JSON.stringify(localStorage).includes('mem_')")
                evaluate(
                    "(() => { const input = "
                    "document.querySelector('select[aria-label=\"Project scope\"]');"
                    "Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value')"
                    ".set.call(input," + json.dumps(other_scope) + ");"
                    "input.dispatchEvent(new Event('change',{bubbles:true})); })()"
                )
                wait("document.querySelector('.token-reveal') === null")
                assert issued not in evaluate("document.body.innerText")
                wait("document.querySelector('input[name=name]') !== null")
                fill("input[name=name]", "Browser second scoped agent")
                wait(
                    "Array.from(document.querySelectorAll('button'))"
                    ".find(b => b.textContent === 'Create scoped credential')?.disabled === false"
                )
                click("Create scoped credential")
                wait("document.querySelector('.token-reveal code') !== null")
                click("I saved it · dismiss")
                assert evaluate("document.querySelector('.token-reveal') === null")
                assert issued not in evaluate("document.body.innerText")
                evaluate(
                    "(() => { const input = "
                    "document.querySelector('select[aria-label=\"Project scope\"]');"
                    "Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value')"
                    ".set.call(input," + json.dumps(personal_scope) + ");"
                    "input.dispatchEvent(new Event('change',{bubbles:true})); })()"
                )
                click("Usage")
                wait("document.querySelector('.usage-table') !== null")
                assert not evaluate("document.querySelector('[role=alert]')?.textContent")
                click("Sources")
                wait("document.querySelector('input[name=document]') !== null")
                document_file = artifacts / "browser-source.md"
                document_file.write_text(
                    "# Browser document\nA document uploaded through the real owner dashboard.\n"
                )
                root = command("DOM.getDocument")["root"]["nodeId"]
                file_node = command(
                    "DOM.querySelector", {"nodeId": root, "selector": "input[name=document]"}
                )["nodeId"]
                command(
                    "DOM.setFileInputFiles", {"nodeId": file_node, "files": [str(document_file)]}
                )
                # The submit button stays disabled while the list is loading;
                # clicking earlier would silently no-op on the disabled button.
                wait(
                    "Array.from(document.querySelectorAll('button')).find("
                    "b => b.textContent === 'Upload document')?.disabled === false"
                )
                click("Upload document")
                wait("document.querySelectorAll('.rows .row').length === 1")
                assert "queued" in evaluate("document.body.innerText")
                click("Process pending jobs")
                wait(
                    "document.querySelector('.processing').innerText"
                    ".includes('No jobs ready to run')"
                )
                wait("document.querySelector('.rows').innerText.includes('ready')")
                assert not evaluate("document.querySelector('[role=alert]')?.textContent")
                screenshot = command("Page.captureScreenshot", {"format": "png"})["data"]
                (artifacts / "sources-desktop.png").write_bytes(base64.b64decode(screenshot))
                command(
                    "Emulation.setDeviceMetricsOverride",
                    {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True},
                )
                wait("document.querySelector('.sidebar').getBoundingClientRect().right <= 0")
                wait("document.querySelector('.sidebar').inert")
                screenshot = command("Page.captureScreenshot", {"format": "png"})["data"]
                (artifacts / "dashboard-mobile.png").write_bytes(base64.b64decode(screenshot))
                assert evaluate(
                    "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
                ), "Mobile layout overflows."
                click("Open navigation")
                wait("document.querySelector('.navigation-open') !== null")
                click("Tasks")
                wait("document.querySelector('h1')?.textContent === 'Tasks'")
                assert not evaluate("document.querySelector('.navigation-open')")
                assert evaluate(
                    "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
                )
                command(
                    "Emulation.setDeviceMetricsOverride",
                    {"width": 320, "height": 844, "deviceScaleFactor": 1, "mobile": True},
                )
                assert evaluate(
                    "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
                ), "320-pixel layout overflows."
                click("Open navigation")
                click("Sign out")
                wait("document.querySelector('input[type=password]') !== null")
                assert not exceptions, "Browser raised JavaScript exceptions."
        finally:
            chrome.terminate()
            try:
                chrome.wait(timeout=5)
            except subprocess.TimeoutExpired:
                chrome.kill()
                chrome.wait(timeout=5)
