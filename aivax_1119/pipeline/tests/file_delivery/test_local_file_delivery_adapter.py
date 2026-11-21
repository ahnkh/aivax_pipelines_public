import asyncio
from pathlib import Path

from src.adapters.delivery_file import LocalFileDeliveryAdapter
from src.domain.delivery_file.models import FileDeliveryRequest


def test_archives_attachment_under_hour_directory_and_removes_source(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "upload-id"
    source_path.write_bytes(b"document")
    adapter = LocalFileDeliveryAdapter(tmp_path / "archive")

    result = asyncio.run(
        adapter.deliver(
            FileDeliveryRequest(
                source_path=source_path,
                file_name="../document.pdf",
            )
        )
    )
    archived = Path(result.destination)

    assert result.delivered
    assert archived.name == "document_upload-id.pdf"
    assert archived.read_bytes() == b"document"
    assert not source_path.exists()
    assert archived.is_relative_to(tmp_path / "archive")
