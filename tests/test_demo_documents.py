"""Demo document set: 12 synthetic templates + 12 completed dummies.

The completed dummies carry the FICTIONAL TRAINING DATA marker and are
registered as mock demo-fixture profiles, so ingesting one classifies to its
listing type without a mock_profile.  Templates carry no marker and are
never fixture-matched.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pypdf import PdfReader

from app.adapters.llm import LLMRequest, MockLLMProvider
from app.domain.enums import DocumentType

DEMO_ROOT = Path(__file__).resolve().parent.parent / "demo_documents"
MARKER = "FICTIONAL TRAINING DATA"

COMPLETED_CASES = [
    ("demo-listing-agreement.pdf", DocumentType.LISTING_AGREEMENT),
    ("demo-seller-advisory.pdf", DocumentType.SELLER_ADVISORY),
    ("demo-agency-disclosure.pdf", DocumentType.AGENCY_DISCLOSURE),
    ("demo-ca-tds.pdf", DocumentType.CA_TDS),
    ("demo-ca-spq.pdf", DocumentType.CA_SPQ),
    ("demo-agent-visual-inspection.pdf", DocumentType.AGENT_VISUAL_INSPECTION),
    ("demo-ca-nhd.pdf", DocumentType.CA_NHD),
    ("demo-wcmd-advisory.pdf", DocumentType.WCMD_ADVISORY),
    ("demo-lead-disclosure.pdf", DocumentType.LEAD_DISCLOSURE),
    ("demo-hoa-package.pdf", DocumentType.HOA_PACKAGE),
    ("demo-prelim-title-report.pdf", DocumentType.PRELIM_TITLE_REPORT),
    ("demo-solar-agreement.pdf", DocumentType.SOLAR_AGREEMENT),
]


def _pdf_text(path: Path) -> str:
    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


@pytest.mark.parametrize("filename,doc_type", COMPLETED_CASES)
def test_completed_dummy_carries_marker_and_fictional_data(filename, doc_type):
    text = _pdf_text(DEMO_ROOT / "completed" / filename)
    assert MARKER in text
    assert "5842-018-024" in text
    assert "Jane Seller" in text
    assert "Not an official California Association of Realtors form" in text


@pytest.mark.parametrize("filename,doc_type", COMPLETED_CASES)
def test_completed_dummy_classifies_via_fixture_profile(filename, doc_type):
    provider = MockLLMProvider()
    text = _pdf_text(DEMO_ROOT / "completed" / filename)
    request = LLMRequest(
        document_name=filename,
        mime_type="application/pdf",
        extracted_text=text,
        extracted_data={},
        metadata={},
    )
    result = provider.analyze(request)
    assert result.document_type is doc_type
    assert result.confidence >= 0.9
    assert result.extracted_fields["apn"] == "5842-018-024"


def test_templates_have_no_marker_and_state_synthetic():
    templates = sorted((DEMO_ROOT / "templates").glob("template-*.pdf"))
    assert len(templates) == 12
    for path in templates:
        text = _pdf_text(path)
        assert MARKER not in text, path.name
        assert "SYNTHETIC TEMPLATE" in text, path.name


def test_completed_set_covers_all_twelve_types():
    completed = sorted((DEMO_ROOT / "completed").glob("demo-*.pdf"))
    assert len(completed) == 12
    assert {path.name for path in completed} == {filename for filename, _ in COMPLETED_CASES}
