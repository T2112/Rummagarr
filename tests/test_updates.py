from __future__ import annotations

import hashlib
import threading
import time

import httpx

from control import WorkerControl
from state import JobStore
from updater import (
    UpdateError,
    check_for_update,
    download_verified,
    installer_launch_args,
    is_newer,
    load_skipped_update,
    parse_release,
    parse_sha256_text,
    parse_version,
    prompt_kind,
    remember_skipped_update,
    verify_installer,
    wait_for_worker_stop,
)


def test_version_comparison_is_numeric():
    assert parse_version("v1.4.0") == parse_version("1.4.0")
    assert parse_version("1.4") == parse_version("1.4.0")
    assert parse_version("1.10.0") > parse_version("1.9.0")
    assert parse_version("1.4.0") > parse_version("1.3.0")
    assert parse_version("1.4.0-rc1") < parse_version("1.4.0")
    assert parse_version("1.4.0-rc2") > parse_version("1.4.0-rc1")
    assert parse_version("1.4.0-rc1") > parse_version("1.3.9")
    assert is_newer("1.10.0", "1.9.0")
    assert is_newer("v1.4.0", "1.3.0")
    assert not is_newer("1.4.0", "1.4.0")
    assert not is_newer("1.3.0", "v1.4.0")
    try:
        parse_version("not-a-version")
    except ValueError:
        pass
    else:
        raise AssertionError("words are not a version")


def _payload(tag: str = "v1.5.0", *, with_installer: bool = True, size: int = 11, notes: str = "Fixed a stall.") -> dict:
    name = f"LidarrSoulseek-Setup-{tag[1:]}.exe"
    assets = [
        {"name": "source.zip", "browser_download_url": "https://example.test/source.zip", "size": 4},
    ]
    if with_installer:
        assets.insert(
            0,
            {
                "name": name,
                "browser_download_url": f"https://example.test/{name}",
                "size": size,
            },
        )
        assets.append(
            {
                "name": name + ".sha256",
                "browser_download_url": f"https://example.test/{name}.sha256",
                "size": 80,
            }
        )
    return {
        "tag_name": tag,
        "html_url": f"https://github.com/T2112/lidarr-soulseek/releases/tag/{tag}",
        "body": notes,
        "assets": assets,
    }


def test_release_parsing_finds_the_installer_and_checksum():
    status = parse_release(_payload(), current="1.4.0", skipped=None, installed=True, platform="nt")
    assert status.latest == "1.5.0"
    assert status.newer
    assert status.notes == "Fixed a stall."
    assert status.page_url.endswith("/v1.5.0")
    assert status.can_install
    assert status.asset_name == "LidarrSoulseek-Setup-1.5.0.exe"
    assert status.asset_size == 11
    assert status.checksum_url.endswith(".sha256")
    assert status.error is None

    same = parse_release(_payload("v1.4.0"), current="1.4.0", skipped=None, installed=True, platform="nt")
    assert not same.newer
    assert prompt_kind(same, manual=True) == "current"
    assert prompt_kind(same, manual=False) == "quiet"

    source = parse_release(_payload(), current="1.4.0", skipped=None, installed=False)
    assert source.newer
    assert not source.can_install
    assert source.error is None
    assert prompt_kind(source, manual=False) == "prompt"

    missing = parse_release(_payload(with_installer=False), current="1.4.0", skipped=None, installed=True, platform="nt")
    assert missing.newer
    assert not missing.can_install
    assert "no LidarrSoulseek-Setup-1.5.0.exe" in (missing.error or "")


def test_skip_version_is_remembered_until_a_newer_release(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    try:
        remember_skipped_update(store, "v1.5.0")
        assert load_skipped_update(store) == "1.5.0"
        skipped = load_skipped_update(store)
        held = parse_release(_payload("v1.5.0"), current="1.4.0", skipped=skipped, installed=False)
        assert held.newer and held.skipped
        assert prompt_kind(held, manual=False) == "quiet"
        assert prompt_kind(held, manual=True) == "prompt"
        newer = parse_release(_payload("v1.6.0"), current="1.4.0", skipped=skipped, installed=False)
        assert newer.newer and not newer.skipped
        assert prompt_kind(newer, manual=False) == "prompt"
        junk = parse_release(_payload("v1.5.0"), current="1.4.0", skipped="nope", installed=False)
        assert junk.newer and not junk.skipped
    finally:
        store.close()


def test_check_reports_rate_limit_and_network_errors():
    def rate_limit(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "API rate limit exceeded for 203.0.113.4."})

    with httpx.Client(transport=httpx.MockTransport(rate_limit)) as client:
        status = check_for_update("1.4.0", client=client, url="https://example.test/latest")
    assert status.error == "GitHub rate limit reached. Try again in a little while."
    assert prompt_kind(status, manual=True) == "error"
    assert prompt_kind(status, manual=False) == "quiet"

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    with httpx.Client(transport=httpx.MockTransport(offline)) as client:
        status = check_for_update("1.4.0", client=client, url="https://example.test/latest")
    assert status.error == "Could not reach GitHub to check for updates."

    def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    with httpx.Client(transport=httpx.MockTransport(missing)) as client:
        status = check_for_update("1.4.0", client=client, url="https://example.test/latest")
    assert "No GitHub release" in (status.error or "")


def test_download_checks_size_and_sha256(tmp_path):
    body = b"installer-bytes"
    digest = hashlib.sha256(body).hexdigest()
    name = "LidarrSoulseek-Setup-1.5.0.exe"
    status = parse_release(
        _payload(size=len(body)),
        current="1.4.0",
        skipped=None,
        installed=True,
        platform="nt",
    )

    def ok(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".sha256"):
            return httpx.Response(200, text=f"{digest}  {name}\n")
        return httpx.Response(200, content=body)

    dest = tmp_path / name
    with httpx.Client(transport=httpx.MockTransport(ok)) as client:
        saved = download_verified(status, dest, client=client)
    assert saved.read_bytes() == body
    assert parse_sha256_text(f"sha256:{digest}") == digest

    def bad_hash(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".sha256"):
            return httpx.Response(200, text=("a" * 64) + f"  {name}\n")
        return httpx.Response(200, content=body)

    wrong = tmp_path / "wrong.exe"
    with httpx.Client(transport=httpx.MockTransport(bad_hash)) as client:
        try:
            download_verified(status, wrong, client=client)
        except UpdateError as exc:
            assert "checksum" in str(exc).lower()
        else:
            raise AssertionError("bad checksum should fail")
    assert not wrong.exists()
    assert not wrong.with_name(wrong.name + ".part").exists()

    short = parse_release(_payload(size=99), current="1.4.0", skipped=None, installed=True, platform="nt")
    with httpx.Client(transport=httpx.MockTransport(ok)) as client:
        try:
            download_verified(short, tmp_path / "short.exe", client=client)
        except UpdateError as exc:
            assert "99" in str(exc)
        else:
            raise AssertionError("size mismatch should fail")


def test_verify_installer_accepts_a_matching_file(tmp_path):
    path = tmp_path / "setup.exe"
    path.write_bytes(b"abc")
    digest = hashlib.sha256(b"abc").hexdigest()
    verify_installer(path, 3, digest)
    try:
        verify_installer(path, 4, digest)
    except UpdateError as exc:
        assert "4" in str(exc)
    else:
        raise AssertionError("wrong size should fail")


def test_installer_launch_is_a_silent_upgrade(tmp_path):
    args = installer_launch_args(tmp_path / "LidarrSoulseek-Setup-1.5.0.exe")
    assert args[0].endswith("LidarrSoulseek-Setup-1.5.0.exe")
    assert "/VERYSILENT" in args
    assert "/CLOSEAPPLICATIONS" in args
    assert "/NORESTART" in args


def test_wait_for_worker_stop_lets_the_thread_finish():
    control = WorkerControl()
    control.start()
    started = threading.Event()

    def run() -> None:
        started.set()
        while not control.is_stopped():
            time.sleep(0.01)

    thread = threading.Thread(target=run)
    thread.start()
    assert started.wait(1)
    assert wait_for_worker_stop(control, thread, timeout=2)
    assert control.is_stopped()
    assert not thread.is_alive()
