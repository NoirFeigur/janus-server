"""Attachment domain: object-storage upload + presigned read.

A cross-cutting upload surface any authenticated user reaches (avatars now,
generic business attachments later). The model/object-key live in ``sys_attach``;
bytes live in the private bucket; reads are short-lived presigned URLs.
"""

from src.files.router import router

__all__ = ["router"]
