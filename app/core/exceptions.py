"""
Custom HTTP exceptions for the API.
"""

from fastapi import HTTPException, status


class FileNotFoundError(HTTPException):
    def __init__(self, file_id: str):
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File with id '{file_id}' not found.",
        )


class UnsupportedFileTypeError(HTTPException):
    def __init__(self, filename: str):
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Unsupported file type for '{filename}'. "
                "Accepted formats: .zip (Shapefile), .kml"
            ),
        )


class FileTooLargeError(HTTPException):
    def __init__(self, max_mb: int):
        super().__init__(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds the maximum allowed size of {max_mb} MB.",
        )


class EmptyFileError(HTTPException):
    def __init__(self, filename: str = "Uploaded file"):
        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{filename} is empty (0 bytes).",
        )


class ProcessingError(HTTPException):
    def __init__(self, detail: str):
        super().__init__(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"File processing failed: {detail}",
        )
