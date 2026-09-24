"""Talking to SharePoint itself: signing in, and putting a file back only if nobody else has.

Microsoft is stubbed at urllib, so what runs here is everything this side of the wire.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path
from typing import Any, Self

import keyring
import keyring.errors
import pytest

from crossing_count import graph, paths

TENANT, CLIENT = "a-tenant-id", "a-client-id"


class _Answer(BytesIO):
    """What urlopen gives back: bytes, headers, and a context manager."""

    def __init__(self, blob: bytes, headers: dict[str, str] | None = None) -> None:
        super().__init__(blob)
        self.headers = headers or {}

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *a: object) -> None:
        return None


class Microsoft:
    """Stands in for Microsoft: answers by URL, and remembers what it was asked."""

    def __init__(self) -> None:
        self.asked: list[dict[str, Any]] = []
        self.replies: dict[str, list[Any]] = {}

    def will(self, fragment: str, *answers: Any) -> None:
        self.replies.setdefault(fragment, []).extend(answers)

    def __call__(self, req: Any, timeout: float = 0, context: Any = None) -> _Answer:
        url = req.full_url
        body = req.data or b""
        self.asked.append({"method": req.get_method(), "url": url,
                           "headers": dict(req.headers), "body": body,
                           "form": dict(urllib.parse.parse_qsl(body.decode("utf8", "replace")))
                           if b"=" in body and b"{" not in body[:1] else {}})
        # the longest fragment that fits wins: "root:/External" and "root:/External:/children"
        # are both in the same URL, and only the longer one means it
        fits = sorted((f for f, q in self.replies.items() if f in url and q), key=len)
        if not fits:
            raise AssertionError(f"nothing queued for {url}")
        nxt = self.replies[fits[-1]].pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        if isinstance(nxt, tuple):
            blob, headers = nxt
            return _Answer(blob if isinstance(blob, bytes) else json.dumps(blob).encode(),
                           headers)
        return _Answer(nxt if isinstance(nxt, bytes) else json.dumps(nxt).encode())


def _http(code: int, said: dict[str, Any] | None = None) -> urllib.error.HTTPError:
    blob = json.dumps(said or {}).encode()
    return urllib.error.HTTPError("https://x", code, "no", {}, BytesIO(blob))  # type: ignore[arg-type]


@pytest.fixture
def ms(monkeypatch: pytest.MonkeyPatch) -> Iterator[Microsoft]:
    store: dict[tuple[str, str], str] = {}

    def delete(s: str, u: str) -> None:
        if (s, u) not in store:
            raise keyring.errors.PasswordDeleteError("Password not found")
        del store[(s, u)]

    monkeypatch.setattr(keyring, "set_password", lambda s, u, p: store.__setitem__((s, u), p))
    monkeypatch.setattr(keyring, "get_password", lambda s, u: store.get((s, u)))
    monkeypatch.setattr(keyring, "delete_password", delete)
    monkeypatch.setattr(paths, "load_settings",
                        lambda: {"graph_tenant": TENANT, "graph_client_id": CLIENT})
    fake = Microsoft()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    fake.store = store  # type: ignore[attr-defined]
    yield fake


def _signed_in(ms: Microsoft) -> None:
    ms.will("oauth2/v2.0/token", {"refresh_token": "r1", "access_token": "a1", "expires_in": 3600})
    ms.will("devicecode", {"user_code": "ABC-DEF", "verification_uri": "https://aka.ms/devicelogin",
                           "device_code": "dev", "interval": 1, "expires_in": 900,
                           "message": "Go to ... and enter ABC-DEF"})
    assert graph.poll(graph.begin()) is True


# ---------- signing in ----------

def test_it_says_what_the_tenant_has_to_register(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths, "load_settings", dict)
    with pytest.raises(graph.NotSignedIn, match="Files.ReadWrite.All"):
        graph.registration()


def test_the_person_is_given_a_code_and_it_waits_for_them(ms: Microsoft) -> None:
    ms.will("devicecode", {"user_code": "ABC-DEF", "verification_uri": "https://aka.ms/devicelogin",
                           "device_code": "dev-code", "interval": 5, "expires_in": 900,
                           "message": "Enter ABC-DEF"})
    started = graph.begin()
    assert started.code == "ABC-DEF" and "devicelogin" in started.url
    assert started.expires_at > time.time()

    ms.will("oauth2/v2.0/token", {"error": "authorization_pending"},
            {"refresh_token": "r1", "access_token": "a1", "expires_in": 3600})
    assert graph.poll(started) is False  # not yet
    assert graph.signed_in() is False
    assert graph.poll(started) is True  # they typed it in
    assert graph.signed_in() is True


def test_a_code_that_ran_out_says_so(ms: Microsoft) -> None:
    ms.will("devicecode", {"user_code": "A", "verification_uri": "u", "device_code": "d"})
    started = graph.begin()
    ms.will("oauth2/v2.0/token", {"error": "expired_token"})
    with pytest.raises(graph.GraphError, match="ran out"):
        graph.poll(started)


def test_the_tokens_go_to_the_credential_store_and_nowhere_else(ms: Microsoft,
                                                                tmp_path: Path) -> None:
    _signed_in(ms)
    kept = [v for (service, _), v in ms.store.items() if "Microsoft" in service]  # type: ignore[attr-defined]
    assert kept and json.loads(kept[0])["refresh_token"] == "r1"
    # nothing of it anywhere on disk: the settings hold the client id, which is not a secret
    assert not any("r1" in p.read_text(errors="ignore")
                   for p in tmp_path.rglob("*") if p.is_file())
    graph.forget()
    assert graph.signed_in() is False
    graph.forget()  # asking twice is not an error


def test_an_access_token_is_reused_until_it_is_nearly_out(ms: Microsoft) -> None:
    _signed_in(ms)
    before = len(ms.asked)
    assert graph.token() == "a1" and graph.token() == "a1"
    assert len(ms.asked) == before  # no asking Microsoft again for one it already has


def test_a_stale_access_token_is_refreshed_without_signing_in_again(ms: Microsoft) -> None:
    _signed_in(ms)
    keyring.set_password(graph._services()[0], graph.ACCOUNT, json.dumps(
        {"refresh_token": "r1", "access_token": "old", "expires_at": time.time() - 1}))
    ms.will("oauth2/v2.0/token", {"access_token": "a2", "expires_in": 3600})
    assert graph.token() == "a2"
    assert ms.asked[-1]["form"]["grant_type"] == "refresh_token"
    # Microsoft does not always send the refresh token back; the one we have is kept
    assert json.loads(keyring.get_password(  # type: ignore[arg-type]
        graph._services()[0], graph.ACCOUNT))["refresh_token"] == "r1"


def test_a_refresh_that_is_refused_asks_for_a_new_sign_in(ms: Microsoft) -> None:
    _signed_in(ms)
    keyring.set_password(graph._services()[0], graph.ACCOUNT, json.dumps(
        {"refresh_token": "r1", "expires_at": 0}))
    ms.will("oauth2/v2.0/token", {"error": "invalid_grant",
                                  "error_description": "token expired"})
    with pytest.raises(graph.NotSignedIn, match="Sign in again"):
        graph.token()


def test_nothing_is_called_before_signing_in(ms: Microsoft) -> None:
    with pytest.raises(graph.NotSignedIn, match="No Microsoft account"):
        graph.read("drive", "x.xlsx")


# ---------- reading and writing a file ----------

def test_the_file_is_read_from_the_server_with_its_etag(ms: Microsoft) -> None:
    _signed_in(ms)
    ms.will("root:/", {"id": "item1", "eTag": '"{GUID},7"', "name": "wb.xlsx"})
    ms.will("/content", (b"a workbook", {}))
    blob, etag = graph.read("drive1", "External/RAG/wb.xlsx")
    assert blob == b"a workbook" and etag == '"{GUID},7"'
    asked = [a["url"] for a in ms.asked]
    assert "External/RAG/wb.xlsx" in urllib.parse.unquote(asked[-2])
    assert ms.asked[-1]["headers"]["Authorization"] == "Bearer a1"


def test_a_folder_with_spaces_in_its_name_is_asked_for_properly(ms: Microsoft) -> None:
    """Every folder on this drive has spaces: "validations - Documents", "Johnny Big"."""
    _signed_in(ms)
    ms.will("root:/", {"id": "i", "eTag": "e"})
    ms.will("/content", (b"x", {}))
    graph.read("d", "External/RAG/Johnny Big/Johnny Big Knox City - JB-204/report.pptx")
    url = [a["url"] for a in ms.asked if "root:/" in a["url"]][-1]
    assert "%20" in url and " " not in url.split("root:/")[1]
    assert urllib.parse.unquote(url).endswith("Johnny Big Knox City - JB-204/report.pptx")


def test_somebody_else_saving_in_between_is_found_out(ms: Microsoft) -> None:
    """The whole reason the eTag is carried: their save is not quietly written over."""
    _signed_in(ms)
    ms.will("root:/", _http(412))
    with pytest.raises(graph.Clash, match="Nothing was written"):
        graph.replace("d", "wb.xlsx", b"new", etag='"{GUID},7"')


def test_the_etag_is_actually_sent(ms: Microsoft) -> None:
    _signed_in(ms)
    ms.will("root:/", {"id": "i"})
    graph.replace("d", "wb.xlsx", b"new", etag='"{GUID},7"')
    assert ms.asked[-1]["headers"]["If-match"] == '"{GUID},7"'
    assert ms.asked[-1]["method"] == "PUT" and ms.asked[-1]["body"] == b"new"


def test_a_workbook_open_in_excel_says_so(ms: Microsoft) -> None:
    _signed_in(ms)
    ms.will("root:/", _http(423))
    with pytest.raises(graph.GraphError, match="open in Excel"):
        graph.replace("d", "wb.xlsx", b"new")


def test_nothing_there_yet_is_not_an_error(ms: Microsoft) -> None:
    _signed_in(ms)
    ms.will("root:/", _http(404))
    assert graph.item("d", "not/there.xlsx") == {}


# ---------- folders and big files ----------

def test_the_folders_above_a_new_one_are_made_too(ms: Microsoft) -> None:
    _signed_in(ms)
    ms.will("root:/External", _http(404))
    ms.will("root/children", {"id": "f1", "name": "External"})
    ms.will("root:/External:/children", {"id": "f2", "name": "RAG"})
    ms.will("root:/External/RAG", _http(404))
    ms.will("root:/External/RAG:/children", {"id": "f3", "name": "Connor Northland - CN-212"})
    ms.will("root:/External/RAG/Connor%20Northland%20-%20CN-212", _http(404))
    made = graph.folder("d", "External/RAG/Connor Northland - CN-212")
    assert made["id"] == "f3"
    made_at = [a for a in ms.asked if a["method"] == "POST" and "children" in a["url"]]
    assert [json.loads(a["body"])["name"] for a in made_at] == [
        "External", "RAG", "Connor Northland - CN-212"]


def test_a_folder_already_there_is_left_alone(ms: Microsoft) -> None:
    _signed_in(ms)
    ms.will("root:/External", {"id": "f1", "folder": {}})
    assert graph.folder("d", "External")["id"] == "f1"
    assert not [a for a in ms.asked if a["method"] == "POST" and "children" in a["url"]]


def test_a_small_file_goes_up_in_one_piece(ms: Microsoft, tmp_path: Path) -> None:
    _signed_in(ms)
    small = tmp_path / "report.pptx"
    small.write_bytes(b"deck" * 100)
    ms.will("root:/", {"id": "up1"})
    seen: list[tuple[int, int]] = []
    assert graph.upload("d", "RAG/report.pptx", small, on=lambda a, b: seen.append((a, b)))["id"] \
        == "up1"
    assert ms.asked[-1]["method"] == "PUT" and ms.asked[-1]["body"] == small.read_bytes()
    assert seen == [(400, 400)]


def test_footage_goes_up_in_pieces_and_says_how_far_it_has_got(ms: Microsoft,
                                                               tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch
                                                               ) -> None:
    """The exports run to 80 MB, well past what one put takes."""
    _signed_in(ms)
    monkeypatch.setattr(graph, "SMALL", 10)
    monkeypatch.setattr(graph, "CHUNK", 10)
    big = tmp_path / "Export - CN-212 marked.mp4"
    big.write_bytes(b"x" * 25)
    ms.will("createUploadSession", {"uploadUrl": "https://upload.example/session"})
    ms.will("upload.example", {}, {}, {"id": "done"})
    seen: list[tuple[int, int]] = []
    assert graph.upload("d", "RAG/Connor/x.mp4", big, on=lambda a, b: seen.append((a, b)))["id"] \
        == "done"
    pieces = [a for a in ms.asked if "upload.example" in a["url"]]
    assert [a["headers"]["Content-range"] for a in pieces] == [
        "bytes 0-9/25", "bytes 10-19/25", "bytes 20-24/25"]
    assert seen == [(10, 25), (20, 25), (25, 25)]
    assert b"".join(a["body"] for a in pieces) == big.read_bytes()


def test_footage_never_goes_over_something_already_there(ms: Microsoft, tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """A delivered folder is the customer's; theirs is renamed around, never replaced."""
    _signed_in(ms)
    monkeypatch.setattr(graph, "SMALL", 1)
    big = tmp_path / "x.mp4"
    big.write_bytes(b"xx")
    ms.will("createUploadSession", {"uploadUrl": "https://upload.example/s"})
    ms.will("upload.example", {"id": "done"})
    graph.upload("d", "RAG/x.mp4", big)
    started = next(a for a in ms.asked if "createUploadSession" in a["url"])
    wanted = json.loads(started["body"])["item"]["@microsoft.graph.conflictBehavior"]
    assert wanted == "rename"


# ---------- finding the site ----------

def test_the_site_is_found_from_a_link_out_of_the_sheet(ms: Microsoft) -> None:
    _signed_in(ms)
    ms.will("/sites/", {"id": "acme.sharepoint.com,guid1,guid2"})
    got = graph.site_id("https://acme.sharepoint.com/sites/validations/Shared%20Documents/x")
    assert got == "acme.sharepoint.com,guid1,guid2"
    assert ms.asked[-1]["url"].endswith("/sites/acme.sharepoint.com:/sites/validations")
