# WIP

CHROME AUTOMATION RULES (CDP)

1. Browser binary
   - Use a NON-snap Chrome/Chromium (snap breaks CDP sandboxing).
   - Point at a direct install: /usr/bin/google-chrome, /usr/bin/chromium, or a downloaded binary. Never use the snap path.

2. Kill existing instances (before launch)
   - pkill -f -e "google-chrome" ; pkill -f -e "chromium" ; pkill -f -e "chrome-headless"
   - Verify none remain before starting.

3. Persistent profile
   - Always launch with: --user-data-dir=$HOME/CHROME_PROFILE

4. Launch flags
   - --remote-debugging-port=9222
   - --no-sandbox (required as root / in containers)
   - Visible window: do NOT pass --headless (headless=False).

5. Protocol
   - Always drive the browser via CDP.
   - GET http://127.0.0.1:9222/json/version → read webSocketDebuggerUrl → use that socket.

6. Network capture
   - Enable Network; listen for Network.RequestWillBeSent and Network.ResponseReceived.
   - Append every event to network_log.json.

7. Navigation
   - Do NOT use Page.navigate and do NOT navigate away from the current page.
   - Interact only through the DOM: create elements/buttons and click them.

8. Readiness
   - Start Chrome, then wait for the START keyword before proceeding.
