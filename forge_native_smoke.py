"""Optional frozen-host smoke fixture: public weights and disposable state only."""
import os
from pathlib import Path
import threading

MODEL = {
    'repo':'Qwen/Qwen3-0.6B-GGUF',
    'revision':'23749fefcc72300e3a2ad315e1317431b06b590a',
    'path':'Qwen3-0.6B-Q8_0.gguf',
    'size':639446688,
    'sha256':'9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031',
}


def frozen_native_smoke(service):
    from forge_native_models import file_hash
    from forge_native_validation import validate_native
    from forge_vendor.strata.frontend import ChatTemplate
    from forge_vendor.strata.tokenizer import Tokenizer,bytes_to_unicode
    from forge_vendor.strata import iq_pack
    from forge_native_mtp import prepare as prepare_mtp
    source=Path(os.environ['FORGE_NATIVE_SMOKE_MODEL']).resolve(strict=True)
    # No arbitrary model code or live workspace data enters this fixture.
    if source.stat().st_size!=MODEL['size'] or file_hash(source)!=MODEL['sha256']:
        raise ValueError('The frozen native smoke model failed its pinned digest.')
    home=service.store.home.resolve()
    if home==(Path.home()/'.forge').resolve():
        raise ValueError('Frozen native smoke requires disposable state.')
    template=home/'strata-smoke.jinja'
    template.write_text('{% for m in messages %}{{ m.content }}{% endfor %}',encoding='utf-8')
    if ChatTemplate(template).render([{'role':'user','content':'Synthetic template'}])!='Synthetic template':
        raise ValueError('The frozen Strata template did not render.')
    tokenizer=Tokenizer(list(bytes_to_unicode().values()),[])
    if tokenizer.decode(tokenizer.encode('Synthetic \U0001f642'))!='Synthetic \U0001f642':
        raise ValueError('The frozen Strata tokenizer did not round-trip.')
    if not callable(iq_pack.main) or not callable(prepare_mtp):raise ValueError('The frozen Strata pack module is unavailable.')
    if not (Path(__file__).parent/'forge_vendor/strata/draft_vocab.bin').is_file():
        raise ValueError('The frozen Strata draft vocabulary is unavailable.')
    if file_hash(Path(__file__).parent/'forge_vendor/strata/QWEN_MTP_LICENSE')!='a0dc422560841fd68e06d974907f8b4c709bca44a67daad2b528437bdf676c08':
        raise ValueError('The frozen Qwen draft license does not match its pinned bytes.')
    runtime=service.providers.native()
    runtime.catalog.configure(str(home/'models'/'native-smoke'))
    model=runtime.catalog.add([source],identifier='native-smoke-qwen',expected=[MODEL])
    runtime.cpu_only=True
    try:checks=validate_native(runtime,model['id'],threading.Event())
    finally:
        runtime.stop()
        runtime.cpu_only=False
    return {**checks,'strata_support_imported':True,'tokenizer_round_trip':True,
            'template_rendered':True,'cpu_only':True,'worker_stopped':runtime.worker is None}
