import zipfile

import pytest

from forge_documents import extract_document
from forge_sources import generated_file
from forge_xml import parse_xml


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-16', 'utf-16-le', 'utf-16-be'])
@pytest.mark.parametrize('declaration', [
    '<!DOCTYPE root>',
    '<!DOCTYPE root [<!ENTITY probe "synthetic">]>',
    '<!DOCTYPE root SYSTEM "file:///synthetic-nonexistent-file">',
    '<!DOCTYPE root [<!ENTITY % remote SYSTEM "https://invalid.example/entity"> %remote;]>',
])
def test_document_types_rejected_across_encodings(encoding, declaration):
    data = (declaration + '<root/>').encode(encoding)
    with pytest.raises(ValueError, match='document type'):
        parse_xml(data)


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-16', 'utf-16-le', 'utf-16-be'])
def test_ordinary_xml_preserves_text_attributes_and_escaped_entities(encoding):
    root = parse_xml('<root value="a &amp; b"><child>Résumé &lt;ok&gt;</child></root>'.encode(encoding))
    assert root.attrib['value'] == 'a & b'
    assert root.find('child').text == 'Résumé <ok>'


def test_comment_containing_doctype_text_is_ordinary_content():
    assert parse_xml(b'<!-- <!DOCTYPE root> --><root/>').tag == 'root'


def test_docx_rejects_entity_before_returning_document_text(tmp_path):
    path = tmp_path / 'synthetic.docx'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml', '<!DOCTYPE w:document [<!ENTITY x "synthetic">]>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body><w:p><w:r><w:t>&x;</w:t></w:r></w:p></w:body></w:document>')
    with pytest.raises(ValueError, match='document type'):
        extract_document(path)


@pytest.mark.parametrize('kind,body', [
    ('junit', '<testsuite><testcase name="synthetic"/></testsuite>'),
    ('coverage_xml', '<coverage version="synthetic"><packages/></coverage>'),
])
def test_report_classification_accepts_plain_xml_but_refuses_dtd(tmp_path, kind, body):
    path = tmp_path / 'report.xml'
    path.write_bytes(body.encode())
    assert generated_file(path, kind)
    path.write_bytes(('<!DOCTYPE ' + ('testsuite' if kind == 'junit' else 'coverage') + '>' + body).encode())
    assert generated_file(path, kind) is False
