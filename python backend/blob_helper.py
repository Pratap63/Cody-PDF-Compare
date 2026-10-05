import os
from io import BytesIO
from azure.storage.blob import BlobServiceClient
from dotenv import load_dotenv

load_dotenv()

AZURE_STORAGE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
AZURE_STORAGE_CONTAINER = os.getenv("AZURE_STORAGE_CONTAINER", "agent0rps")

if not AZURE_STORAGE_CONNECTION_STRING:
    raise RuntimeError("[Error] AZURE_STORAGE_CONNECTION_STRING missing in .env")

_blob_service_client = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
_container_client = _blob_service_client.get_container_client(AZURE_STORAGE_CONTAINER)

_pdf_bytes_cache = {}
_MAX_CACHE_ENTRIES = 20


def upload_pdf_to_blob(blob_name: str, file_bytes: bytes) -> str:
    blob_client = _container_client.get_blob_client(blob_name)
    blob_client.upload_blob(file_bytes, overwrite=True)
    return blob_name


def download_pdf_bytes(blob_name: str) -> bytes:
    if blob_name in _pdf_bytes_cache:
        return _pdf_bytes_cache[blob_name]

    blob_client = _container_client.get_blob_client(blob_name)
    data = blob_client.download_blob().readall()

    if len(_pdf_bytes_cache) >= _MAX_CACHE_ENTRIES:
        _pdf_bytes_cache.pop(next(iter(_pdf_bytes_cache)))
    _pdf_bytes_cache[blob_name] = data

    return data


def clear_pdf_cache():
    _pdf_bytes_cache.clear()

def verify_blob_upload(blob_name: str) -> dict:
    try:
        blob_client = _container_client.get_blob_client(blob_name)

        properties = blob_client.get_blob_properties()

        return {
            "exists": True,
            "blob_name": blob_name,
            "size_bytes": properties.size,
            "content_type": properties.content_settings.content_type,
            "last_modified": str(properties.last_modified)
        }

    except Exception as e:
        return {
            "exists": False,
            "blob_name": blob_name,
            "error": str(e)
        }