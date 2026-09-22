"""Best-effort applicant allotment lookups against IPO registrars.

This subsystem is separate from the PDF/LLM analysis pipeline. It never sends
applicant identifiers to a model and never reads or writes the analysis cache.
"""

from .base import AllotmentProvider, ClientFactory
from .service import AllotmentService, build_allotment_service

__all__ = [
    "AllotmentProvider",
    "AllotmentService",
    "ClientFactory",
    "build_allotment_service",
]
