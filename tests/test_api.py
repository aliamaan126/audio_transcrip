from pathlib import Path

import pytest
from httpx import AsyncClient

from tests.conftest import SAMPLE_WAV

pytestmark = pytest.mark.usefixtures("stub_ffmpeg")


async def upload(
    client: AsyncClient,
    headers: dict[str, str],
    source: Path = SAMPLE_WAV,
    filename: str = "speech.wav",
    **fields: str,
):
    return await client.post(
        "/v1/transcriptions",
        files={"file": (filename, source.read_bytes(), "application/octet-stream")},
        data=fields,
        headers=headers,
    )


async def test_health_needs_no_credentials(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_upload_rejects_missing_api_key(client: AsyncClient) -> None:
    response = await upload(client, headers={})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_upload_rejects_unknown_api_key(client: AsyncClient) -> None:
    response = await upload(client, headers={"X-API-Key": "nope"})
    assert response.status_code == 401


async def test_upload_rejects_unsupported_content(
    client: AsyncClient, auth_headers: dict[str, str], tmp_path: Path
) -> None:
    disguised = tmp_path / "not-really.wav"
    disguised.write_bytes(b"%PDF-1.7\n" + b"0" * 128)

    response = await upload(client, auth_headers, source=disguised)

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media_type"


async def test_upload_rejects_empty_file(
    client: AsyncClient, auth_headers: dict[str, str], tmp_path: Path
) -> None:
    empty = tmp_path / "empty.wav"
    empty.write_bytes(b"")

    response = await upload(client, auth_headers, source=empty)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "empty_file"


async def test_upload_rejects_invalid_callback_url(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await upload(client, auth_headers, callback_url="not-a-url")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_callback_url"


async def test_upload_then_poll_returns_a_completed_transcript(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    created = await upload(client, auth_headers)
    assert created.status_code == 202
    job_id = created.json()["job_id"]

    polled = await client.get(f"/v1/transcriptions/{job_id}", headers=auth_headers)
    body = polled.json()

    assert polled.status_code == 200
    assert body["status"] == "completed"
    assert body["duration"] == pytest.approx(6.0, abs=0.05)
    assert body["full_text"]
    assert body["completed_at"] is not None
    assert body["segments"][0]["id"] == 0
    assert 0.0 <= body["segments"][0]["confidence"] <= 1.0


async def test_identical_upload_returns_the_existing_job(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    first = await upload(client, auth_headers)
    second = await upload(client, auth_headers, filename="same-audio-different-name.wav")

    assert second.status_code == 200
    assert second.json()["job_id"] == first.json()["job_id"]


@pytest.mark.parametrize(
    ("fmt", "expected"),
    [("srt", "00:00:00,000 -->"), ("vtt", "WEBVTT"), ("json", '"segments"')],
)
async def test_export_renders_each_format(
    client: AsyncClient, auth_headers: dict[str, str], fmt: str, expected: str
) -> None:
    job_id = (await upload(client, auth_headers)).json()["job_id"]

    response = await client.get(
        f"/v1/transcriptions/{job_id}/export", params={"format": fmt}, headers=auth_headers
    )

    assert response.status_code == 200
    assert expected in response.text


async def test_export_rejects_unknown_format(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    job_id = (await upload(client, auth_headers)).json()["job_id"]

    response = await client.get(
        f"/v1/transcriptions/{job_id}/export", params={"format": "docx"}, headers=auth_headers
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_retry_rejects_a_completed_job(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    job_id = (await upload(client, auth_headers)).json()["job_id"]

    response = await client.post(f"/v1/transcriptions/{job_id}/retry", headers=auth_headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_retryable"


async def test_delete_removes_job_and_stored_audio(
    client: AsyncClient, auth_headers: dict[str, str], local_storage
) -> None:
    job_id = (await upload(client, auth_headers)).json()["job_id"]

    deleted = await client.delete(f"/v1/transcriptions/{job_id}", headers=auth_headers)
    polled = await client.get(f"/v1/transcriptions/{job_id}", headers=auth_headers)

    assert deleted.status_code == 204
    assert polled.status_code == 404
    assert not (local_storage.root / "jobs" / job_id).exists()


async def test_unknown_job_returns_a_structured_404(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.get(
        "/v1/transcriptions/00000000-0000-0000-0000-000000000000", headers=auth_headers
    )

    assert response.status_code == 404
    assert response.json()["error"] == {
        "code": "not_found",
        "message": "Job 00000000-0000-0000-0000-000000000000 does not exist",
    }
