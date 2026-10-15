"""XML for inspected documents and reports; document types grant no authority."""
from xml.etree import ElementTree


class _DocumentTree(ElementTree.TreeBuilder):
    def doctype(self, name, public_id, system_id):
        raise ValueError('XML document type declarations are not supported.')


def parse_xml(data):
    if not isinstance(data, bytes) or len(data) > 64 * 1024 * 1024:
        raise ValueError('XML input exceeds the inspection limit.')
    return ElementTree.fromstring(data, parser=ElementTree.XMLParser(target=_DocumentTree()))
