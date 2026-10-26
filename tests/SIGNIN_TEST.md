# Sign-in test through a real browser

The unit tests (`tests/test_auth.py`) cover the sign-in store and provider. This test covers what they cannot: a real assistant, a real browser, the catalogue's own keys page. Run it once per release that touches sign-in, about 10 minutes.

You need a staff account on the home catalogue and an assistant that can add a custom connector (a browser-based one).

1. **Make a test key.** On the catalogue, open `/api/keys` and create a key named "sign-in test" with **Read** only. Copy it.
2. **Connect.** In the assistant, add a custom connector with the address `https://mcp.theahg.co.za/mcp` and choose **Connect**. The page "Connect your assistant to ..." opens. Paste the test key and choose **Connect**. You should be back in the assistant, connected, within a few seconds.
3. **Use it.** Ask: "Using AHG Archives, search for Engelbrecht and give me the links." Links starting `https://psis.theahg.co.za/` should come back.
4. **Check a wrong key is refused.** Add the connector a second time (or disconnect and connect again), paste a made-up key, and choose **Connect**. The page should say "That key was not accepted by the catalogue" and stay on the page.
5. **Revoke the key.** Back on `/api/keys`, revoke the "sign-in test" key.
6. **Use it again.** Ask the same question. The assistant should now report that the catalogue refused it (not authorised), not return records.
7. **Check what the service recorded.** On the server:

       cd ~/glam-mcp && python3 tests/signin_check.py

   Paste the test key when asked (it is not shown, and only its fingerprint is printed). Both checks should say PASS: an ok call before revoking, refused calls after. Within an hour the access token expires, and its refresh is refused because the key is revoked; running the script again then shows no live tokens.
8. Remove the connector from the assistant.

Record the date, the assistant used and the result in the release notes.
