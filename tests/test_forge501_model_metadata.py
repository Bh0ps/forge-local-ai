import hashlib

import numpy as np
import pytest
from gguf import GGUFWriter

from forge_models import ModelManager


def write_model(path, template):
    writer = GGUFWriter(path, 'llama')
    writer.add_uint32('llama.embedding_length', 4)
    if template is not None:
        writer.add_string('tokenizer.chat_template', template)
    writer.add_string('tokenizer.ggml.model', 'llama')
    writer.add_tensor('fixture.weight', np.zeros((4, 4), dtype=np.float32))
    writer.write_header_to_file()
    writer.write_kv_data_to_file()
    writer.write_tensors_to_file()
    writer.close()


def test_model_template_identity_is_verified_without_dumping_template(tmp_path):
    path = tmp_path / 'model.gguf'
    template = '{% for message in messages %}{{ message.content }}{% endfor %}'
    write_model(path, template)
    metadata = ModelManager._metadata(path)
    assert metadata['chat_template_present']
    assert metadata['chat_template_sha256'] == hashlib.sha256(template.encode()).hexdigest()
    assert metadata['tokenizer_model'] == 'llama'
    assert template not in str(metadata)


@pytest.mark.parametrize('template', [' ', 'x' * 262145], ids=['empty', 'unbounded'])
def test_empty_or_unbounded_template_cannot_be_imported(tmp_path, template):
    path = tmp_path / 'model.gguf'
    write_model(path, template)
    with pytest.raises(ValueError, match='chat template'):
        ModelManager._metadata(path)


def test_missing_template_is_explicit_for_runtime_validation(tmp_path):
    path = tmp_path / 'model.gguf'
    write_model(path, None)
    metadata = ModelManager._metadata(path)
    assert metadata['chat_template_present'] is False
    assert 'chat_template_sha256' not in metadata
