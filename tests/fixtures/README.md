# Test fixtures

## `gitcode_login_code.png`

A real login code captured from GitCode's create-QR endpoint
(`POST /uc/api/v1/qrcode/wechat_mini_program`), kept because three assertions in
`tests/test_gitcode_qr.py::MiniProgramCodeTest` are only meaningful against a
genuine artefact:

1. it has no QR finder patterns;
2. its finest dark feature is far below one QR module;
3. a real QR decoder returns nothing for it.

A synthetic stand-in would test our own image generator rather than the thing
GitCode actually sends, which is the opposite of the point.

### Why committing it is safe

It is a **pre-authentication** artefact, not a credential:

* it is the code a user *scans to start* a login, so it grants nothing on its
  own -- anyone who scanned it would be authenticating as themselves;
* it is single-use and expires within minutes; this one was captured well over
  an hour before it was committed;
* the scene behind it was **never scanned or fulfilled** -- it was captured by a
  script to inspect the wire format, and the session it belonged to ended
  without it;
* it contains no token, cookie, account name or identifier for the person who
  requested it. It is an opaque scene reference plus branding.

If this fixture is ever regenerated, treat the new capture with the same care:
capture it without scanning it, and let it expire before committing.

### Regenerating

```bash
python tools/verify_qr_render.py   # prints the structural facts; does not save
```

To capture a fresh fixture, save the decoded bytes from
`decode_payload_image(challenge.image)` -- and note that the properties asserted
against it are the *point* of the fixture, so a new capture must still exhibit
them.
