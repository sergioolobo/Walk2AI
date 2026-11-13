"""Audit trail."""

from app.services.audit.service import RequestMeta, record_audit

__all__ = ["RequestMeta", "record_audit"]
