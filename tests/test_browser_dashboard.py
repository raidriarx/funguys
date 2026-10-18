"""Real Chromium checks against an offline-capable local dashboard."""
import socket
import threading
import time
import uuid
import pytest
import uvicorn
from playwright.sync_api import sync_playwright, expect
from funguys.host.app import create_host_app


def test_dashboard_login_dirty_edits_approval_and_reconnect(tmp_path):
    app=create_host_app(tmp_path,admin_token='browser-admin')
    listener=socket.socket()
    listener.bind(('127.0.0.1',0))
    port=listener.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error'))
    thread=threading.Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True)
    thread.start()
    deadline=time.monotonic()+10
    while not server.started and time.monotonic()<deadline:
        time.sleep(0.02)
    assert server.started
    errors=[]
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True)
            try:
                page=browser.new_page()
                page.on('pageerror',lambda exc:errors.append(str(exc)))
                page.route('https://**/*',lambda route:route.abort())
                page.goto(f'http://127.0.0.1:{port}')
                expect(page.locator('#login')).to_be_visible()
                page.locator('#login-token').fill('browser-admin')
                page.get_by_role('button',name='Sign in',exact=True).click()
                expect(page.locator('#app')).to_be_visible()
                expect(page.locator('#connection')).to_have_text('LIVE')
                draft='# User contract draft\n\nDo not overwrite this unsaved text.'
                page.locator('#contracts').fill(draft)
                response=page.request.post(f'http://127.0.0.1:{port}/api/agents/register',data={'owner_name':'UI Friend','machine_name':'<script>bad()</script>','profile_key':'gemini-cli','worker_id':uuid.uuid4().hex})
                assert response.ok,response.text()
                expect(page.locator('#agents')).to_contain_text('UI Friend')
                expect(page.locator('#contracts')).to_have_value(draft)
                page.locator('#approve').click()
                expect(page.locator('#project-status')).to_have_text('APPROVED')
                assert app.state.store.get_project().contracts_markdown==draft
                page.evaluate('socket.close()')
                expect(page.locator('#connection')).to_have_text('LIVE',timeout=10000)
                page.locator('#pause').click()
                expect(page.locator('#project-status')).to_have_text('PAUSED / REVIEW REQUIRED')
                page.locator('#sign-out').click()
                expect(page.locator('#login')).to_be_visible()
                assert not errors,errors
            finally:
                browser.close()
    finally:
        server.should_exit=True
        thread.join(timeout=10)
        listener.close()
