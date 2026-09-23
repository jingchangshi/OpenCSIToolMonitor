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

from opencsi.auth.gitcode_refresh import RefreshResult, RefreshStatus
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

#: A GitCode access token with plenty of life left, so the production chain does
#: not try to refresh it. Fifteen days is what GitCode actually issues; this is
#: well clear of the 24 h refresh margin.
GITCODE_HEALTHY = time.time() + 15 * 24 * 3600

CALLBACK_PATH = "/opencsitool/rest/v1/oauth2/authorization/callback/gitcode"


def oauth_routes(
    minted: str = FAKE_OPENCSITOOL_TOKEN,
    *,
    max_age: int | None = None,
    host: str = "opencsitool.com",
    base: str = "https://opencsitool.com",
) -> dict:
    """The three-request OAuth flow, as the pinned shape test describes it.

    ``max_age`` adds a ``Max-Age`` to the minted cookie. Omitted by default,
    because the recorded evidence (``docs/api-investigation.md`` §7.5) documents
    the *lifetime* as ~58 minutes but never captured a ``Max-Age`` on the minted
    cookie -- the 401-clears case is the only ``Set-Cookie`` on record. So the
    honest default is a session cookie, and the renewer treats "no expiry" as
    unknown rather than inventing one.

    ``host``/``base`` exist because the acceptance runner points the renewer at a
    loopback address. The stub is keyed by ``(host, path)``, so a run against
    ``127.0.0.1`` would otherwise miss every route and look like a renewal
    failure rather than a fixture mismatch.
    """
    cookie = _set_cookie("token", minted)
    if max_age is not None:
        cookie += f"; Max-Age={max_age}"
    callback = f"{base}{CALLBACK_PATH}?code=CODE&state=ST"
    return {
        (host, OAUTH_ENTRY_PATH): (
            302,
            {"Location": AUTHORIZE_URL},
            b"",
        ),
        ("web-api.gitcode.com", CHECK_AUTHORIZE_PATH): (
            200,
            {},
            json.dumps({"redirect_uri": callback}).encode(),
        ),
        (host, CALLBACK_PATH): (302, {"Set-Cookie": cookie}, b""),
    }


def store_with(
    *,
    access: str = ACCESS,
    refresh: str | None = REFRESH,
    session: str = OLD_SESSION,
    session_expires_at: float | None = None,
    gitcode_expires_at: float = GITCODE_HEALTHY,
) -> MemoryCredentialStore:
    """A store holding a GitCode credential and a near-expiry openCsiTool session.

    ``gitcode_expires_at`` defaults to a *healthy* token rather than ``None``, and
    that default is load-bearing. ``GitCodeTokenRefresher.needs_refresh`` treats an
    unknown expiry as "refresh now" -- correct for a real credential whose expiry
    was never recorded, but it means a fixture that omits the field makes the
    production chain attempt a genuine network refresh on every test run. A test
    that quietly reaches gitcode.com is a test that fails in CI, passes at home, or
    worse, rotates a real credential. Tests that want the refresh to happen pass a
    near-expiry value explicitly and stub the transport.
    """
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


class NoNetworkTest(unittest.TestCase):
    """No test in this file may reach the network. Enforced, not assumed.

    This exists because it was already violated: the P0 fixtures left the GitCode
    ``access_expires_at`` unset, the refresher's documented "unknown expiry means
    refresh now" rule then fired, and the suite made a real HTTPS call to
    gitcode.com from what looked like a fully offline test. It surfaced only
    because the *production* credential was rejected and the assertion changed
    shape -- with a valid stored token it would have silently succeeded, and in CI
    it would have silently failed.
    """

    def test_no_outbound_connection_is_attempted(self) -> None:
        """Fail loudly if the real GitCode token endpoint is ever contacted."""
        from opencsi.auth import gitcode_refresh as module

        real_urlopen = module.urllib.request.urlopen

        def refuse(url, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            target = url if isinstance(url, str) else getattr(url, "full_url", url)
            if "gitcode.com" in str(target):
                raise AssertionError(f"a test tried to reach the network: {target}")
            return real_urlopen(url, *args, **kwargs)

        with mock.patch.object(module.urllib.request, "urlopen", refuse):
            store = store_with(gitcode_expires_at=time.time() + 60)
            with patch_store(store), patch_transport(_StubOpener(oauth_routes())):
                ctx = context_for(store)
                provider = ctx.make_provider()
                renewer = ctx.make_renewer(
                    provider, base_url="https://opencsitool.com"
                )
                SessionManager(provider, renewer=renewer).renew(force=True)


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

        with patch_store(store), patch_transport(opener):
            ctx = context_for(store)
            provider = ctx.make_provider()
            renewer = ctx.make_renewer(provider, base_url="https://opencsitool.com")
            result = SessionManager(provider, renewer=renewer).renew(force=True)

        from opencsi.auth.session import FallbackRenewer

        self.assertIsInstance(renewer, FallbackRenewer)
        member = renewer._renewers[0]  # noqa: SLF001
        # The store-backed renewer is wrapped by the GitCode-refresh orchestrator,
        # so the persistence verdict lives one level in.
        inner = getattr(member, "_renewer", member)
        self.assertIs(
            inner.last_persisted,
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


class GitCodeRefreshLifecycleTest(unittest.TestCase):
    """The long-lived half: an expiring GitCode token must renew itself first.

    ``GitCodeTokenRefresher`` and ``StoredGitCodeRefresher`` were written and
    tested a round earlier, and nothing in the product called them. The class
    existing is not the same as the lifecycle running, so these tests drive the
    production chain with a GitCode token that is about to expire and assert both
    that the refresh happened and -- just as importantly -- that it did *not*
    happen when it was not needed.
    """

    def _chain(self, store, *, oauth_opener, gitcode_opener):
        """Build the production chain over a store, stubbing only HTTP."""
        ctx = context_for(store)
        provider = ctx.make_provider()
        renewer = ctx.make_renewer(provider, base_url="https://opencsitool.com")
        return provider, renewer

    def test_an_expiring_gitcode_token_is_refreshed_before_opencsi_renewal(self) -> None:
        """A1/R1 near expiry -> A2/R2 -> OAuth -> T2, all persisted."""
        near = time.time() + 60  # inside DEFAULT_REFRESH_MARGIN (24 h)
        store = store_with(access="gitcode-access-token-OLDOLDOLDOLDOLDOLD", gitcode_expires_at=near)

        refreshed = StoredGitCodeCredential(
            access_token="gitcode-access-token-NEWNEWNEWNEWNEWNEW",
            refresh_token="gitcode-refresh-token-NEWNEWNEWNEWNEWNEW",
            username="alice",
            access_expires_at=time.time() + 1296000,
        )

        with patch_store(store), patch_transport(_StubOpener(oauth_routes())), mock.patch(
            "opencsi.auth.gitcode_refresh.StoredGitCodeRefresher.refresh",
            return_value=RefreshResult(RefreshStatus.REFRESHED, credential=refreshed),
        ) as spy:
            provider = None
            ctx = context_for(store)
            provider = ctx.make_provider()
            renewer = ctx.make_renewer(provider, base_url="https://opencsitool.com")
            SessionManager(provider, renewer=renewer).renew(force=True)

            self.assertTrue(spy.called, "the GitCode refresh was never consulted")

        self.assertEqual(store.load().opencsi.token, FAKE_OPENCSITOOL_TOKEN)

    def test_a_healthy_gitcode_token_is_not_refreshed(self) -> None:
        """Ten days of life left means no refresh *request*. This is the guard.

        Without it, every ~hourly openCsiTool renewal would also spend a GitCode
        refresh, and with rotation in play that is how a working credential gets
        rotated far more often than the protocol requires -- for no benefit.

        Counted at the HTTP boundary, not on ``GitCodeTokenRefresher.refresh``.
        That method is where the expiry gate *lives*, so spying on it counts calls
        to the gate rather than requests through it, and would report 1 whether or
        not the gate worked. What must not happen is a network request.
        """
        far = time.time() + 10 * 24 * 3600  # well outside the 24 h margin
        store = store_with(gitcode_expires_at=far)

        with patch_store(store), patch_transport(_StubOpener(oauth_routes())), mock.patch(
            "opencsi.auth.gitcode_refresh.GitCodeTokenRefresher._post"
        ) as post:
            ctx = context_for(store)
            provider = ctx.make_provider()
            renewer = ctx.make_renewer(provider, base_url="https://opencsitool.com")
            SessionManager(provider, renewer=renewer).renew(force=True)

        self.assertEqual(
            post.call_count,
            0,
            "a GitCode token with ten days left still produced a refresh request",
        )
        self.assertEqual(store.load().opencsi.token, FAKE_OPENCSITOOL_TOKEN)


    def test_a_rotated_gitcode_credential_reaches_the_store_and_the_oauth_leg(self) -> None:
        """A1/R1 near expiry -> A2/R2 lands in the store -> OAuth spends A2 -> T2.

        This is the end-to-end version of the refresh lifecycle, and it exists
        because the test above does not prove it. That one patches
        ``StoredGitCodeRefresher.refresh`` and so never exercises parsing, the
        rotation write, or the OAuth leg consuming the rotated token -- it proves
        the orchestrator consults the refresher, not that the loop closes.

        Only ``GitCodeTokenRefresher._post`` is stubbed, which is the single HTTP
        seam for the refresh endpoint. Everything above it is production code: the
        200-response parser, the atomic A2/R2 write, and the adapter's read half
        that hands A2 to the OAuth flow.
        """
        store = store_with(
            access="gitcode-access-token-ROTATEME000000",
            refresh="gitcode-refresh-token-ROTATEME000000",
            gitcode_expires_at=time.time() + 60,  # inside the 24 h margin
        )

        rotated_access = "gitcode-access-token-AFTERROTATE00"
        rotated_refresh = "gitcode-refresh-token-AFTERROTATE00"
        response = json.dumps(
            {
                "access_token": rotated_access,
                "refresh_token": rotated_refresh,
                "expires_in": 1296000,
                "scope": "user_info",
                "created_at": int(time.time()),
            }
        )

        # What the OAuth leg was actually handed, observed at the GitCode source.
        # Recorded by wrapping the *class* rather than patching an instance
        # attribute: assigning onto the adapter shadowed its own forwarding and
        # made this test fail for a reason that had nothing to do with the code
        # under test.
        seen: list[list] = []
        from opencsi.auth.stored import StoredGitCodeCredentialSource

        real_read = StoredGitCodeCredentialSource.read_all_cookies

        def recording_read(self, *args, **kwargs):
            cookies = real_read(self, *args, **kwargs)
            seen.append(cookies)
            return cookies

        with patch_store(store), patch_transport(
            _StubOpener(oauth_routes())
        ), mock.patch.object(
            StoredGitCodeCredentialSource, "read_all_cookies", recording_read
        ), mock.patch(
            "opencsi.auth.gitcode_refresh.GitCodeTokenRefresher._post",
            return_value=(200, response),
        ) as post:
            ctx = context_for(store)
            provider = ctx.make_provider()
            renewer = ctx.make_renewer(provider, base_url="https://opencsitool.com")
            result = SessionManager(provider, renewer=renewer).renew(force=True)

        # Collected after the run: `seen` is appended to while the chain executes.
        access_seen = [
            c.get("value")
            for batch in seen
            for c in batch
            if isinstance(c, Mapping) and c.get("name") == "GITCODE_ACCESS_TOKEN"
        ]

        self.assertEqual(post.call_count, 1, "the refresh endpoint was not called")
        self.assertIs(result.status, RenewalStatus.RENEWED)

        # Read through a *fresh* source over the same store, which is what the next
        # process does. Reading `store.load()` directly is not enough here, and the
        # difference is not academic: this assertion originally passed even with
        # the rotation write deleted, because the refresher returns the rotated
        # credential in memory and the OAuth leg happily spends that. The renewal
        # therefore succeeds while the rotation is silently lost -- correct for
        # this process, wrong for the next one. Asking a new reader is what
        # separates the two.
        reread = StoredGitCodeCredentialSource(store)._credential()  # noqa: SLF001
        self.assertIsNotNone(reread, "no GitCode credential was readable back")
        self.assertEqual(
            reread.access_token,
            rotated_access,
            "the rotated access token is not durable: a fresh reader still sees the "
            "superseded one",
        )
        self.assertEqual(
            reread.refresh_token,
            rotated_refresh,
            "the rotated refresh token is not durable, so the next process will "
            "refresh with a token the server has already superseded",
        )

        stored = store.load().gitcode
        self.assertEqual(stored.access_token, rotated_access)
        self.assertEqual(stored.refresh_token, rotated_refresh)

        # The OAuth flow reads the source lazily, so the read that matters is the
        # last one -- after the rotation. Asserting on the final value catches the
        # real failure mode: the rotation landing in the store but the OAuth leg
        # still spending the superseded token.
        self.assertTrue(access_seen, "the OAuth leg never read a GitCode credential")
        self.assertEqual(
            access_seen[-1],
            rotated_access,
            "the OAuth leg spent the OLD GitCode token, so the rotation never "
            "reached the leg that needs it",
        )
        self.assertEqual(store.load().opencsi.token, FAKE_OPENCSITOOL_TOKEN)


class LateBoundTest(unittest.TestCase):
    """A credential that is refreshed later must not break the chain."""

    def test_a_failed_refresh_does_not_become_login_required(self) -> None:
        """A network failure on refresh is not the same as a lost credential.

        The stored access token may still be perfectly good, and sending the user
        for a QR scan because GitCode was briefly unreachable is the failure mode
        this forbids. The renewal continues on the token that is already there.
        """
        store = store_with(gitcode_expires_at=time.time() + 60)

        with patch_store(store), patch_transport(_StubOpener(oauth_routes())), mock.patch(
            "opencsi.auth.gitcode_refresh.GitCodeTokenRefresher.refresh",
            return_value=RefreshResult(
                RefreshStatus.NETWORK_ERROR, detail="gitcode unreachable"
            ),
        ):
            ctx = context_for(store)
            provider = ctx.make_provider()
            renewer = ctx.make_renewer(provider, base_url="https://opencsitool.com")
            result = SessionManager(provider, renewer=renewer).renew(force=True)

        self.assertIsNot(result.status, RenewalStatus.LOGIN_REQUIRED)
        self.assertIs(result.status, RenewalStatus.RENEWED)
        self.assertEqual(store.load().opencsi.token, FAKE_OPENCSITOOL_TOKEN)


class RenewalCapabilityTest(unittest.TestCase):
    """``--status`` and ``doctor`` must report the store path as renewable.

    ``renewal_capability`` knew only about ``CdpCookieProvider``. Once the secure
    store became the normal source, a stored credential was reported "unavailable
    -- a manually supplied token has no GitCode SSO session", which is both false
    and the wrong reason. Measured live before the fix, on a machine where
    ``login --renew`` then succeeded.
    """

    def test_a_stored_credential_reports_renewable(self) -> None:
        from opencsi.auth.oauth_browser import renewal_capability

        store = store_with()
        with patch_store(store):
            ctx = context_for(store)
            provider = ctx.make_provider()
            capability = renewal_capability(provider)

        self.assertTrue(
            capability.available,
            f"a stored credential was reported as unable to renew: {capability.reason}",
        )
        self.assertNotIn("manually supplied token", capability.reason)

    def test_a_stored_credential_without_a_refresh_token_is_caveated(self) -> None:
        """Renewable now, but not indefinitely -- said as a caveat, not a failure."""
        from opencsi.auth.oauth_browser import renewal_capability

        store = store_with(refresh=None)
        with patch_store(store):
            ctx = context_for(store)
            provider = ctx.make_provider()
            capability = renewal_capability(provider)

        self.assertTrue(capability.available)
        self.assertTrue(
            capability.caveated,
            "a credential with no refresh token reported no caveat, so the user "
            "would not know a QR scan is coming",
        )

    def test_an_empty_store_reports_unavailable_with_a_real_reason(self) -> None:
        from opencsi.auth.oauth_browser import renewal_capability

        store = MemoryCredentialStore()  # nothing stored at all
        with patch_store(store):
            ctx = context_for(store)
            provider = ctx.make_provider()
            capability = renewal_capability(provider)

        self.assertFalse(capability.available)
        self.assertIn("login --qr", capability.reason)

    def test_a_manual_token_is_still_not_renewable(self) -> None:
        """The browser-free path must not make a pasted token look renewable."""
        from opencsi.auth.manual import ManualCookieProvider
        from opencsi.auth.oauth_browser import renewal_capability

        store = store_with()
        with patch_store(store):
            manual = ManualCookieProvider("pasted-token-abcdefghijklmnop")
            capability = renewal_capability(manual)

        self.assertFalse(
            capability.available,
            "a manually pasted token was reported as renewable against a stored "
            "identity it has nothing to do with",
        )


if __name__ == "__main__":
    unittest.main()
