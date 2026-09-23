"""The production renewal chain: does a renewed session reach the next process?

The gap this file exists to catch
---------------------------------
``HttpOAuthRenewer`` has always assumed its single ``provider`` argument is *both*
the GitCode credential source **and** the new openCsiTool token sink. It reads the
GitCode cookies through ``read_all_cookies()`` and writes the minted session back
through ``remember_token()`` / ``install_token()``, each reached with ``getattr``
so that a provider without them is silently tolerated::

    remember = getattr(self._provider, "remember_token", None)
    if callable(remember):
        ...
    else:
        log.debug("the credential provider cannot cache a token; ...")

``CdpCookieProvider`` satisfies both roles. ``StoredGitCodeCredentialSource``
satisfies only the first -- it is read-only by design, because a GitCode
credential source has no business writing an openCsiTool session. So once
``make_renewer()`` started handing it to ``HttpOAuthRenewer``, renewal minted a new
session on the server and dropped it on the floor, one ``log.debug`` deep.

That is invisible to every test that calls ``StoredOpenCsiCredentialProvider``
directly, which is the whole point: the sink worked, the *wiring* did not. So these
tests drive the real seams -- ``CliContext.make_provider()``,
``CliContext.make_renewer()``, ``SessionManager``, ``HttpOAuthRenewer`` -- and
stub only the HTTP transport, which is the one thing that cannot be real here.

What is deliberately *not* stubbed
----------------------------------
An earlier draft of this concern was answered with "the provider's
``remember_token`` works", which proves nothing about production. Every assertion
below is made against a freshly constructed provider reading the same store, i.e.
what a second process does. The store is a ``MemoryCredentialStore`` shared by
object identity within one test; ``tools/acceptance_durable_renewal.py`` repeats it
across three real OS processes.
"""

from __future__ import annotations

import json
import sys
import time
import unittest
from typing import Any, Mapping
from unittest import mock

import helpers  # noqa: F401  (imported for its sys.path side effect)

from opencsi.auth.http_oauth import (
    CHECK_AUTHORIZE_PATH,
    OAUTH_ENTRY_PATH,
    HttpOAuthRenewer,
)
from opencsi.auth.session import RenewalStatus, SessionManager
from opencsi.auth.store import (
    MemoryCredentialStore,
    StoredGitCodeCredential,
    StoredOpenCsiCredential,
)

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from test_http_oauth import (  # noqa: E402  (reusing the pinned flow fixtures)
    AUTHORIZE_URL,
    CALLBACK_URL,
    FAKE_OPENCSITOOL_TOKEN,
    _StubOpener,
    _set_cookie,
)

ACCESS = "gitcode-access-token-abcdefghijklmnop"
REFRESH = "gitcode-refresh-token-abcdefghijklmnop"
OLD_SESSION = "opencsi-session-old-abcdefghijklmnop"

CALLBACK_PATH = "/opencsitool/rest/v1/oauth2/authorization/callback/gitcode"


def oauth_routes(
    minted: str = FAKE_OPENCSITOOL_TOKEN, *, max_age: int | None = None
) -> dict:
    """The three-request OAuth flow, as the pinned shape test describes it.

    ``max_age`` adds a ``Max-Age`` to the minted cookie. Omitted by default,
    because the recorded evidence (``docs/api-investigation.md`` §7.5) documents
    the *lifetime* as ~58 minutes but never captured a ``Max-Age`` on the minted
    cookie -- the 401-clears case is the only ``Set-Cookie`` on record. So the
    honest default is a session cookie, and the renewer treats "no expiry" as
    unknown rather than inventing one.
    """
    cookie = _set_cookie("token", minted)
    if max_age is not None:
        cookie += f"; Max-Age={max_age}"
    return {
        ("opencsitool.com", OAUTH_ENTRY_PATH): (
            302,
            {"Location": AUTHORIZE_URL},
            b"",
        ),
        ("web-api.gitcode.com", CHECK_AUTHORIZE_PATH): (
            200,
            {},
            json.dumps({"redirect_uri": CALLBACK_URL}).encode(),
        ),
        ("opencsitool.com", CALLBACK_PATH): (302, {"Set-Cookie": cookie}, b""),
    }


def store_with(
    *,
    access: str = ACCESS,
    refresh: str | None = REFRESH,
    session: str = OLD_SESSION,
    session_expires_at: float | None = None,
    gitcode_expires_at: float | None = None,
) -> MemoryCredentialStore:
    """A store holding a GitCode credential and a near-expiry openCsiTool session."""
    store = MemoryCredentialStore()
    store.save_gitcode(
        StoredGitCodeCredential(
            access_token=access,
            refresh_token=refresh,
            username="alice",
            access_expires_at=gitcode_expires_at,
        )
    )
    store.save_opencsi(
        StoredOpenCsiCredential(
            token=session,
            expires_at=(
                time.time() - 60
                if session_expires_at is None
                else session_expires_at
            ),
        )
    )
    return store


def context_for(store: MemoryCredentialStore, *, cdp: str | None = None):
    """A real ``CliContext`` whose secure store is ``store``.

    ``open_default_store`` is patched rather than ``make_stored_provider``: the
    method under test is the *provider construction*, so replacing it would
    remove the thing being verified. Only the file handle is substituted, which
    is also what keeps these tests off the developer's real credential file.
    """
    import io

    from opencsi.cli.context import CliContext

    class _Args:
        cdp = None
        base_url = None
        json = False
        no_proxy = True
        no_store = False
        store_ttl = 5.0
        renew_timeout = 5.0
        ports = None

    args = _Args()
    args.cdp = cdp
    return CliContext(args=args, stdout=io.StringIO(), stderr=io.StringIO())


def patch_store(store: MemoryCredentialStore):
    """Make ``open_default_store()`` return ``store`` for the duration."""
    return mock.patch(
        "opencsi.auth.windows_store.open_default_store", return_value=store
    )


def patch_transport(opener: _StubOpener):
    """Replace only the HTTP layer of ``HttpOAuthRenewer``.

    Reaches in through the same private seam ``test_http_oauth`` uses. The point
    of the exercise is that everything *above* the socket is production code, so
    the socket is the correct thing to cut.
    """
    return mock.patch.object(
        HttpOAuthRenewer,
        "_opener",
        lambda self, jar: opener.for_jar(jar),
    )


class ProductionRenewalChainTest(unittest.TestCase):
    """``make_renewer`` must persist what it renews. This is P0."""

    def test_production_renewal_chain_persists_the_new_session(self) -> None:
        """The whole point: T1 in, T2 out, and a fresh provider can see T2.

        Drives ``make_provider`` -> ``make_renewer`` -> ``SessionManager.renew``
        with the real ``HttpOAuthRenewer``; only HTTP is scripted.
        """
        store = store_with()
        opener = _StubOpener(oauth_routes())

        with patch_store(store), patch_transport(opener):
            ctx = context_for(store)
            provider = ctx.make_provider()
            renewer = ctx.make_renewer(provider, base_url="https://opencsitool.com")
            self.assertIsNotNone(renewer, "the stored path must offer a renewer")

            session = SessionManager(provider, renewer=renewer)
            result = session.renew(force=True)

        self.assertIs(result.status, RenewalStatus.RENEWED)

        # The assertion that was failing before the fix: a *new* provider, built
        # from the same store, must read the token the server just minted.
        with patch_store(store):
            from opencsi.auth.stored import StoredOpenCsiCredentialProvider

            reader = StoredOpenCsiCredentialProvider(store)
            self.assertEqual(
                reader.get_token(),
                FAKE_OPENCSITOOL_TOKEN,
                "the renewed session was not persisted, so the next process "
                "would still read the old one",
            )

    def test_the_gitcode_credential_survives_the_renewal(self) -> None:
        """Persisting the session must not disturb the other half of the bundle."""
        store = store_with()
        with patch_store(store), patch_transport(_StubOpener(oauth_routes())):
            ctx = context_for(store)
            provider = ctx.make_provider()
            renewer = ctx.make_renewer(provider, base_url="https://opencsitool.com")
            SessionManager(provider, renewer=renewer).renew(force=True)

        bundle = store.load()
        self.assertIsNotNone(bundle.gitcode)
        self.assertEqual(bundle.gitcode.access_token, ACCESS)
        self.assertEqual(bundle.gitcode.refresh_token, REFRESH)
        self.assertEqual(bundle.gitcode.username, "alice")

    def test_the_normal_path_contacts_no_browser(self) -> None:
        """A stored credential must not make the renewer reach for a browser."""
        store = store_with()

        from opencsi.auth.cdp import CdpCookieProvider

        def refuse(*_args: Any, **_kwargs: Any):
            raise AssertionError("a browser was contacted on the stored path")

        with patch_store(store), patch_transport(_StubOpener(oauth_routes())), mock.patch.object(
            CdpCookieProvider, "_read_cookies", refuse
        ):
            ctx = context_for(store)
            provider = ctx.make_provider()
            renewer = ctx.make_renewer(provider, base_url="https://opencsitool.com")
            result = SessionManager(provider, renewer=renewer).renew(force=True)

        self.assertIs(result.status, RenewalStatus.RENEWED)

    def test_a_dated_cookie_stores_its_expiry(self) -> None:
        """When the server *does* date the cookie, the expiry must be recorded.

        The renewer derives ``expires_in`` from the response's ``Max-Age`` and
        hands it to the provider. Losing it would leave the next renewal decision
        with nothing to compare against, so the session would be renewed on every
        run instead of shortly before it dies.
        """
        store = store_with()
        with patch_store(store), patch_transport(
            _StubOpener(oauth_routes(max_age=3480))
        ):
            ctx = context_for(store)
            provider = ctx.make_provider()
            renewer = ctx.make_renewer(provider, base_url="https://opencsitool.com")
            SessionManager(provider, renewer=renewer).renew(force=True)

        stored = store.load().opencsi
        self.assertIsNotNone(stored)
        self.assertIsNotNone(
            stored.expires_at,
            "the server dated the cookie but the expiry was not persisted",
        )
        self.assertGreater(stored.expires_at, time.time())

    def test_an_undated_cookie_is_stored_without_inventing_an_expiry(self) -> None:
        """No ``Max-Age`` means unknown, and unknown must not become a guess.

        The recorded evidence never captured a ``Max-Age`` on the minted cookie,
        so this is the case that can actually happen. Writing ``now + 3600`` here
        would make the renewal decision trust a number the server never sent;
        ``None`` is the honest value, and ``SessionManager`` already treats it as
        "renew when asked" rather than "still valid".
        """
        store = store_with()
        with patch_store(store), patch_transport(_StubOpener(oauth_routes())):
            ctx = context_for(store)
            provider = ctx.make_provider()
            renewer = ctx.make_renewer(provider, base_url="https://opencsitool.com")
            SessionManager(provider, renewer=renewer).renew(force=True)

        stored = store.load().opencsi
        self.assertIsNotNone(stored)
        self.assertEqual(stored.token, FAKE_OPENCSITOOL_TOKEN)
        self.assertIsNone(
            stored.expires_at,
            "an expiry was invented for a cookie the server did not date",
        )

    def test_a_successful_persist_is_not_reported_as_a_failure(self) -> None:
        """``last_persisted`` must be ``True`` when the store write succeeded.

        The renewer records ``bool(install_token(...))``. A sink that writes
        correctly but returns ``None`` makes a durable renewal report itself as
        "valid for this process only", which is the same wrong answer the P0
        defect produced -- by a different route.
        """
        store = store_with()
        opener = _StubOpener(oauth_routes())
        captured: dict = {}

        with patch_store(store), patch_transport(opener):
            ctx = context_for(store)
            provider = ctx.make_provider()
            renewer = ctx.make_renewer(provider, base_url="https://opencsitool.com")
            result = SessionManager(provider, renewer=renewer).renew(force=True)

        from opencsi.auth.session import FallbackRenewer

        self.assertIsInstance(renewer, FallbackRenewer)
        http_member = renewer._renewers[0]  # noqa: SLF001
        captured["last_persisted"] = http_member.last_persisted
        self.assertIs(
            captured["last_persisted"],
            True,
            "a successfully persisted session was reported as not persisted",
        )
        self.assertTrue(result.ok)


class IdentityIsolationTest(unittest.TestCase):
    """Renewal must never write one identity's session into another's slot."""

    def test_a_manual_token_gets_no_renewer(self) -> None:
        """A pasted token has no upstream session, so it must not borrow one."""
        store = store_with()
        with patch_store(store):
            ctx = context_for(store)
            from opencsi.auth.manual import ManualCookieProvider

            manual = ManualCookieProvider("pasted-token-abcdefghijklmnop")
            self.assertIsNone(
                ctx.make_renewer(manual, base_url="https://opencsitool.com")
            )

    def test_an_explicit_cdp_provider_does_not_write_the_stored_identity(self) -> None:
        """``--cdp`` means "use this browser", not "renew whoever is in the store"."""
        store = store_with()
        opener = _StubOpener(oauth_routes("session-minted-from-the-browser-aaaa"))

        with patch_store(store), patch_transport(opener):
            ctx = context_for(store, cdp="http://127.0.0.1:9222")
            provider = ctx.make_provider()

            # Whatever the composite returns, the *explicit* CDP provider is the
            # active identity, so the store must not be used as its sink.
            from opencsi.auth.cdp import CdpCookieProvider

            self.assertIsInstance(provider, CdpCookieProvider)

            renewer = ctx.make_renewer(provider, base_url="https://opencsitool.com")
            self.assertIsNotNone(renewer)

            # Renew against a scripted browser session, and assert the stored
            # openCsiTool session is untouched afterwards.
            with mock.patch.object(
                CdpCookieProvider,
                "read_all_cookies",
                lambda self, timeout=None: [
                    {
                        "name": "GITCODE_ACCESS_TOKEN",
                        "value": ACCESS,
                        "domain": ".gitcode.com",
                        "path": "/",
                        "secure": True,
                        "expires": 0,
                    }
                ],
            ), mock.patch.object(
                CdpCookieProvider, "peek_token", lambda self: None
            ), mock.patch.object(
                CdpCookieProvider, "get_token", lambda self: None
            ), mock.patch.object(
                CdpCookieProvider, "remember_token", lambda self, t, **k: None
            ):
                SessionManager(provider, renewer=renewer).renew(force=True)

        self.assertEqual(
            store.load().opencsi.token,
            OLD_SESSION,
            "a session minted for an explicitly chosen browser was written into "
            "the stored identity",
        )


if __name__ == "__main__":
    unittest.main()
